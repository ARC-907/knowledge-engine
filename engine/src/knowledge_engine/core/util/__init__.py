"""Small, dependency-free helpers shared by every engine subsystem.

Each concern lives in its own module so importing one never drags in the
others (nothing here does I/O at import time):

* :mod:`.time`    -- ``utc_now_iso()``: the one timestamp format the engine stores.
* :mod:`.hashing` -- ``sha256_hex()`` / ``sha256_digest()`` for text, bytes and files.
* :mod:`.slug`    -- ``slugify()``: folder/project names to ``a-z0-9-`` slugs.
* :mod:`.env`     -- ``env_bool()`` / ``env_list()``: the shared environment parsers
  (``core.settings.Settings`` uses the same ones).
* :mod:`.proc`    -- ``run_git()``: the single ``git`` subprocess runner.
* :mod:`.sqlite`  -- ``connect()`` with the engine's standard pragmas,
  ``transaction()`` and per-thread connection caches.
* :mod:`.vectors` -- ``pack_vector()`` / ``unpack_vector()`` (the one embedding blob
  format) and ``cosine()``, which refuses vectors of different dimensions.
"""

from __future__ import annotations
