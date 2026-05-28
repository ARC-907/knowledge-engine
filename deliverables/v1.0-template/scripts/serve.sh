#!/usr/bin/env bash
# Knowledge Engine - serve dashboard + API
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT="$(pwd)"
export KE_CORPUS_ROOT="$ROOT/corpus"
export KE_DATA_DIR="$ROOT/engine/data"
export KE_REGISTRY_PATH="$ROOT/corpus/registry.json"

# shellcheck disable=SC1091
. engine/.venv/bin/activate

PORT="${KE_PORT:-9210}"
echo "Knowledge Engine serving at http://127.0.0.1:$PORT"
echo "  - Dashboard: http://127.0.0.1:$PORT/ui/"
echo "  - API docs:  http://127.0.0.1:$PORT/docs"
echo
exec uvicorn knowledge_engine.app:create_app --factory --host 127.0.0.1 --port "$PORT"
