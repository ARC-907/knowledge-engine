"""Cosine-similarity search over the embedding index built by build.py.

Loads chunk embeddings from SQLite, embeds the query via Ollama, returns the
top-k most-similar chunks ranked by cosine similarity.

Usage:
    python -m knowledge_engine.embeddings.search "your query" --top 10
"""

from __future__ import annotations

import argparse
import sqlite3

from .build import (
    DB_PATH,
    blob_to_embedding,
    cosine_similarity,
    get_embedding,
)


def search(query: str, top: int = 10, library: str | None = None) -> list[dict]:
    """Return the top-N most-similar chunks for ``query``.

    Parameters
    ----------
    query : str
        Free-text query to embed and search against.
    top : int
        Max number of hits to return.
    library : str, optional
        If set, restrict to chunks whose file is in this top-level corpus subdirectory.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"No embedding index at {DB_PATH}. Run `python -m knowledge_engine.embeddings.build` first."
        )

    q_emb = get_embedding(query)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if library:
        rows = conn.execute(
            """
            SELECT c.text AS text, c.heading AS heading, c.embedding AS embedding,
                   f.rel_path AS rel_path, f.library AS library
              FROM chunks c
              JOIN files f ON f.id = c.file_id
             WHERE f.library = ?
            """,
            (library,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT c.text AS text, c.heading AS heading, c.embedding AS embedding,
                   f.rel_path AS rel_path, f.library AS library
              FROM chunks c
              JOIN files f ON f.id = c.file_id
            """
        ).fetchall()

    scored = []
    for row in rows:
        emb = blob_to_embedding(row["embedding"])
        score = cosine_similarity(q_emb, emb)
        scored.append(
            {
                "score": score,
                "rel_path": row["rel_path"],
                "library": row["library"],
                "heading": row["heading"],
                "text": row["text"],
            }
        )

    scored.sort(key=lambda r: r["score"], reverse=True)
    conn.close()
    return scored[:top]


def main() -> None:
    parser = argparse.ArgumentParser(description="Cosine search over the corpus embedding index.")
    parser.add_argument("query", help="The free-text query")
    parser.add_argument("--top", type=int, default=10, help="Top-N hits to return")
    parser.add_argument("--library", default=None, help="Restrict to a corpus subdirectory")
    args = parser.parse_args()

    hits = search(args.query, top=args.top, library=args.library)
    if not hits:
        print("No hits.")
        return

    for i, h in enumerate(hits, 1):
        snippet = h["text"].replace("\n", " ")[:200]
        print(f"[{i:2}] score={h['score']:.3f}  {h['rel_path']}  ({h['heading']})")
        print(f"     {snippet}...")
        print()


if __name__ == "__main__":
    main()
