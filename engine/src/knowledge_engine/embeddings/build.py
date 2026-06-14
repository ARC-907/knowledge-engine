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
    KE_CORPUS_ROOT     (default: ./corpus)
    KE_EMBEDDINGS_DB   (default: $KE_DATA_DIR/embeddings.db or ./engine/data/embeddings.db)
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_URL = os.environ.get("KE_OLLAMA_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("KE_EMBED_MODEL", "bge-m3:latest")

CORPUS_ROOT = Path(
    os.environ.get("KE_CORPUS_ROOT", str(Path.cwd() / "corpus"))
).resolve()

_default_db_dir = Path(os.environ.get("KE_DATA_DIR", str(Path.cwd() / "engine" / "data")))
DB_PATH = Path(
    os.environ.get("KE_EMBEDDINGS_DB", str(_default_db_dir / "embeddings.db"))
).resolve()

CHUNK_SIZE = 1500   # chars per chunk (bge-m3 context is ~8192 tokens)
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
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()[:16]


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


def get_embedding(text: str) -> list[float]:
    """Fetch a single embedding from Ollama. Raises on HTTP error."""
    body = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/embed", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode())
    return data["embeddings"][0]


def embedding_to_blob(emb: list[float]) -> bytes:
    """Pack a float list to compact binary (4 bytes per float)."""
    import struct

    return struct.pack(f"{len(emb)}f", *emb)


def blob_to_embedding(blob: bytes) -> list[float]:
    """Unpack a binary blob back to a float list."""
    import struct

    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-dimension vectors."""
    dot = sum(x * y for x, y in zip(a, b))
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
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    _init_db(conn)

    # Gather all markdown files
    md_files: list[Path] = []
    for md_path in CORPUS_ROOT.rglob("*.md"):
        parts = md_path.relative_to(CORPUS_ROOT).parts
        if any(p in SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        md_files.append(md_path)

    print(f"Found {len(md_files)} markdown files under {CORPUS_ROOT}")

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

        file_id = conn.execute(
            "SELECT id FROM files WHERE rel_path=?", (rel,)
        ).fetchone()[0]

        # Replace chunks for this file
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))

        for ci, chunk in enumerate(chunks):
            try:
                emb = get_embedding(chunk["text"][:2000])
                blob = embedding_to_blob(emb)
                conn.execute(
                    """
                    INSERT INTO chunks (file_id, chunk_idx, text, heading, embedding)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (file_id, ci, chunk["text"], chunk["heading"], blob),
                )
                total_chunks += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  Embed error on {rel} chunk {ci}: {exc}")
                errors += 1

        conn.commit()
        indexed += 1

        if (i + 1) % 20 == 0 or i == len(md_files) - 1:
            elapsed = time.time() - start_time
            rate = indexed / elapsed if elapsed > 0 else 0
            print(
                f"  [{i+1}/{len(md_files)}] {indexed} indexed, "
                f"{skipped} skipped, {errors} errors, "
                f"{total_chunks} chunks ({rate:.1f} files/sec)"
            )

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Files indexed: {indexed}")
    print(f"  Files skipped: {skipped}")
    print(f"  Total chunks: {total_chunks}")
    print(f"  Errors: {errors}")
    print(f"  DB: {DB_PATH} ({DB_PATH.stat().st_size / 1024 / 1024:.1f} MB)")
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

    conn = sqlite3.connect(str(DB_PATH))
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
    import argparse

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
