"""Knowledge-Engine tools layer (opt-in).

Tool host: registry of addressable tools that agents can discover and invoke.
Three tool kinds — `script` (run a command), `service` (proxy to an upstream
HTTP service), `static` (serve a file or directory).

Depends on `knowledge_engine.foundation` (config + db).
"""

from . import host

__all__ = ["host"]
