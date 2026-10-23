"""Embedding index builder for the Knowledge-Engine corpus.

Indexes all markdown files under the corpus root using an Ollama embedding model
(bge-m3 by default). Stores chunks + embeddings in SQLite so the engine can serve
fast cosine-similarity search alongside the FTS5 keyword index.

Usage:
    python -m knowledge_engine.embeddings.build                # Full rebuild
    python -m knowledge_engine.embeddings.build --incremental  # Only changed
    python -m knowledge_engine.embeddings.build --stats        # Stats

Env vars:
    KE_OLLAMA_URL      (default: http://127.0.0.1:11434)
    KE_EMBED_MODEL     (default: bge-m3:latest)
    KE_CORPUS_ROOT     (default: $KE_HOME/corpus -- see core/paths.py)
    KE_EMBEDDINGS_DB   (default: <data_dir>/embeddings.db; data_dir is $KE_DATA_DIR
                        or $KE_HOME/.knowledge-engine/data)
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path

from ...core import paths as engine_paths
from ...core.ollama import OllamaClient, OllamaError
from ...core.util.hashing import sha256_hex
from ...core.util.sqlite import connect as sqlite_connect

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL = os.environ.get("KE_OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("KE_EMBED_MODEL", "bge-m3:latest")

_paths = engine_paths.resolve(warn_missing_corpus=False)  # import time: stay quiet
CORPUS_ROOT = _paths.corpus_root
DB_PATH = Path(os.environ.get("KE_EMBEDDINGS_DB", str(_paths.data_dir / "embeddings.db"))).resolve()

#: Seconds allowed for one ``/api/embed`` request.
EMBED_TIMEOUT = 60.0

CHUNK_SIZE = 1500  # chars per chunk (bge-m3 context is ~8192 tokens)
CHUNK_OVERLAP = 200

# Dirs to skip during corpus walk
SKIP_DIRS = {
    ".history",
    ".vscode",
    "node_modules",
    "__pycache__",
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
}


def _init_db(conn: sqlite3.Connection) -> None:
    """Create the files and chunks tables if they don't exist."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            rel_path TEXT UNIQUE NOT NULL,
            file_hash TEXT NOT NULL,
            library TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            total_chunks INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
            chunk_idx INTEGER NOT NULL,
            text TEXT NOT NULL,
            heading TEXT,
            embedding BLOB NOT NULL,
            UNIQUE(file_id, chunk_idx)
        );
        CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
        CREATE INDEX IF NOT EXISTS idx_files_lib ON files(library);
    """
    )


def _file_hash(path: Path) -> str:
    """Compute a truncated SHA-256 hash of a file's contents."""
    return sha256_hex(path, length=16)


def _chunk_text(text: str, path_rel: str) -> list[dict]:
    """Split text into overlapping chunks, tracking nearest heading."""
    lines = text.split("\n")
    chunks: list[dict] = []
    current_heading = path_rel
    buf: list[str] = []
    buf_len = 0

    for line in lines:
        if line.startswith("#"):
            current_heading = line.lstrip("#").strip()

        buf.append(line)
        buf_len += len(line) + 1

        if buf_len >= CHUNK_SIZE:
            chunk_body = "\n".join(buf)
            chunks.append({"text": chunk_body, "heading": current_heading})
            # Keep overlap window
            overlap_chars = 0
            overlap_start = len(buf)
            for i in range(len(buf) - 1, -1, -1):
                overlap_chars += len(buf[i]) + 1
                if overlap_chars >= CHUNK_OVERLAP:
                    overlap_start = i
                    break
            buf = buf[overlap_start:]
            buf_len = sum(len(line) + 1 for line in buf)

    if buf:
        chunks.append({"text": "\n".join(buf), "heading": current_heading})

    return chunks


def _client() -> OllamaClient:
    return OllamaClient(OLLAMA_URL, EMBED_TIMEOUT)


def get_embeddings(texts: list[str]) -> list[list[float]]:
    """Embed ``texts`` with ``KE_EMBED_MODEL`` (batched). Raises ``OllamaError``."""
    return _client().embed(texts, EMBED_MODEL)


def get_embedding(text: str) -> list[float]:
    """Fetch a single embedding from Ollama. Raises ``OllamaError`` on failure."""
    return get_embeddings([text])[0]


def _embed_chunks(rel: str, chunks: list[dict]) -> list[list[float] | None]:
    """Embed every chunk of one file; a chunk that fails is ``None`` (logged).

    One batched request per file; if it fails, each chunk is retried alone so
    a single bad chunk does not drop the whole file.
    """
    texts = [chunk["text"][:2000] for chunk in chunks]
    try:
        return list(get_embeddings(texts))
    except OllamaError as exc:
        _logger.debug("batched embed failed for %s (%s); retrying per chunk", rel, exc)
    out: list[list[float] | None] = []
    for ci, text in enumerate(texts):
        try:
            out.append(get_embedding(text))
        except OllamaError as exc:
            _logger.warning("embed error on %s chunk %d: %s", rel, ci, exc)
            out.append(None)
    return out


def embedding_to_blob(emb: list[float]) -> bytes:
    """Pack a float list to compact binary (4 bytes per float)."""
    return struct.pack(f"{len(emb)}f", *emb)


def blob_to_embedding(blob: bytes) -> list[float]:
    """Unpack a binary blob back to a float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-dimension vectors."""
    # strict=False keeps the historical truncating behaviour: a query embedded with a
    # different model than the index can differ in dimension, and search must not raise.
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _find_library(rel_path: str) -> str:
    """Extract the top-level corpus subdirectory ('library name') from a relative path."""
    parts = Path(rel_path).parts
    if parts:
        return parts[0]
    return "unknown"


def build_index(incremental: bool = False) -> dict:
    """Build or update the embedding index. Returns a stats dict."""
    conn = sqlite_connect(DB_PATH)
    _init_db(conn)

    # Gather all markdown files
    md_files: list[Path] = []
    for md_path in CORPUS_ROOT.rglob("*.md"):
        parts = md_path.relative_to(CORPUS_ROOT).parts
        if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        md_files.append(md_path)

    _logger.info("found %d markdown files under %s", len(md_files), CORPUS_ROOT)

    existing: dict[str, str] = {}
    if incremental:
        for row in conn.execute("SELECT rel_path, file_hash FROM files"):
            existing[row[0]] = row[1]

    indexed = 0
    skipped = 0
    errors = 0
    total_chunks = 0
    start_time = time.time()

    for i, md_path in enumerate(md_files):
        rel = str(md_path.relative_to(CORPUS_ROOT)).replace("\\", "/")
        fhash = _file_hash(md_path)

        if incremental and rel in existing and existing[rel] == fhash:
            skipped += 1
            continue

        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            errors += 1
            continue

        if len(text.strip()) < 50:
            skipped += 1
            continue

        chunks = _chunk_text(text, rel)
        library = _find_library(rel)

        # Upsert file record
        conn.execute(
            """
            INSERT INTO files (rel_path, file_hash, library, indexed_at, total_chunks)
            VALUES (?, ?, ?, datetime('now'), ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                file_hash=excluded.file_hash,
                library=excluded.library,
                indexed_at=excluded.indexed_at,
                total_chunks=excluded.total_chunks
            """,
            (rel, fhash, library, len(chunks)),
        )

        file_id = conn.execute("SELECT id FROM files WHERE rel_path=?", (rel,)).fetchone()[0]

        # Replace chunks for this file
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))

        for ci, (chunk, emb) in enumerate(zip(chunks, _embed_chunks(rel, chunks), strict=True)):
            if emb is None:
                errors += 1
                continue
            conn.execute(
                """
                INSERT INTO chunks (file_id, chunk_idx, text, heading, embedding)
                VALUES (?, ?, ?, ?, ?)
                """,
                (file_id, ci, chunk["text"], chunk["heading"], embedding_to_blob(emb)),
            )
            total_chunks += 1

        conn.commit()
        indexed += 1

        if (i + 1) % 20 == 0 or i == len(md_files) - 1:
            elapsed = time.time() - start_time
            rate = indexed / elapsed if elapsed > 0 else 0
            _logger.info(
                "[%d/%d] %d indexed, %d skipped, %d errors, %d chunks (%.1f files/sec)",
                i + 1,
                len(md_files),
                indexed,
                skipped,
                errors,
                total_chunks,
                rate,
            )

    elapsed = time.time() - start_time
    _logger.info(
        "done in %.1fs: %d files indexed, %d skipped, %d chunks, %d errors; DB %s (%.1f MB)",
        elapsed,
        indexed,
        skipped,
        total_chunks,
        errors,
        DB_PATH,
        DB_PATH.stat().st_size / 1024 / 1024,
    )
    conn.close()

    return {
        "indexed": indexed,
        "skipped": skipped,
        "errors": errors,
        "chunks": total_chunks,
        "elapsed_seconds": elapsed,
        "db_path": str(DB_PATH),
    }


def show_stats() -> None:
    """Print index statistics."""
    if not DB_PATH.exists():
        print("No index found. Run `python -m knowledge_engine.embeddings.build` first.")
        return

    conn = sqlite_connect(DB_PATH, row_factory=None)
    files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    libs = conn.execute(
        "SELECT library, COUNT(*), SUM(total_chunks) FROM files GROUP BY library ORDER BY COUNT(*) DESC"
    ).fetchall()

    print(f"Embedding Index: {DB_PATH}")
    print(f"  Total files: {files}")
    print(f"  Total chunks: {chunks}")
    print(f"  DB size: {DB_PATH.stat().st_size / 1024 / 1024:.1f} MB")
    print("\n  Per top-level corpus subdirectory:")
    for lib, count, chunk_count in libs:
        print(f"    {lib}: {count} files, {chunk_count} chunks")

    conn.close()


def main() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Build / inspect the corpus embedding index.")
    parser.add_argument("--incremental", action="store_true", help="Only index new/changed files")
    parser.add_argument("--stats", action="store_true", help="Show index statistics and exit")
    args = parser.parse_args()

    if args.stats:
        show_stats()
    else:
        build_index(incremental=args.incremental)


if __name__ == "__main__":
    main()
