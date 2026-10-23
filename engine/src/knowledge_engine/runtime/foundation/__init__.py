"""Foundation layer for the opt-in pipeline subsystem.

`foundation/config.py` is the YAML+env-var config loader.
`foundation/db/`       is the SQLite WAL backbone (bounded per-thread connection
                       cache, JSON auto-deserialization, numbered schema
                       migrations in `db/schema/NNN_*.sql`).

Config knobs:
  - `KE_PIPELINE_ROOT` env var sets the pipeline directory (default: `./pipeline`);
    its `.env` is loaded by `config.load_dotenv_file()` from the entry points,
    never at import.
  - `KE_PIPELINE_DB` env var sets the SQLite path (default: `$KE_DATA_DIR/pipeline.db`).

The schema is a lean coordination store: queue, worker registry, message board,
hosted tools, chat. Buyers seed their own chat-persona content; the table is
empty on first boot.
"""
