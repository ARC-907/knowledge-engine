# Local routing (opt-in)

Local provider implementations. Only imported if the user opts in via
`pip install knowledge-engine[local]` and Ollama is reachable.

Add a provider here, register it in your own `app.py` override, or expose it
via env (`KE_LOCAL_OLLAMA_URL`, `KE_LOCAL_OLLAMA_MODEL`).
