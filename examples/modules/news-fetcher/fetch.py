"""Reference news/media fetcher module (script transport, protocol 1).

Reads one JSON request on stdin and writes one JSON response on stdout.
Inputs are *approved* only: ``file://`` URLs under a configured fixture root
(offline, the default), or ``http(s)`` URLs on an allowlisted host when the
operator sets ``allow_live_http``. A fixture ``<name>`` may have a sidecar
``<name>.meta.json`` with ``canonical_url``, ``title``, ``published_at`` and
``rights`` (recorded metadata; nothing is invented).

Standard library only.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

MAX_BYTES = 2 * 1024 * 1024
MEDIA_TYPES = {".html": "text/html", ".htm": "text/html", ".md": "text/markdown", ".txt": "text/plain"}


def fail(code: str, message: str) -> None:
    json.dump({"error": {"code": code, "message": message}}, sys.stdout)
    sys.exit(0)


def from_fixture(url: str, settings: dict) -> tuple[bytes, str, dict]:
    # url2pathname maps "/D:/dir/x" to "D:\\dir\\x" on Windows and unquotes.
    raw_path = url2pathname(urlsplit(url).path)
    module_dir = Path.cwd().resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = module_dir / candidate
    candidate = candidate.resolve()
    roots = [(module_dir / root).resolve() for root in settings.get("fixture_roots") or ["fixtures"]]
    if not any(candidate.is_relative_to(root) for root in roots):
        fail("denied", "file inputs must come from a configured fixture root")
    if not candidate.is_file():
        fail("invalid_input", "fixture not found")
    data = candidate.read_bytes()[: MAX_BYTES + 1]
    if len(data) > MAX_BYTES:
        fail("invalid_input", "fixture too large")
    meta_path = candidate.with_name(candidate.name + ".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    media_type = MEDIA_TYPES.get(candidate.suffix.lower(), "application/octet-stream")
    return data, media_type, meta


def from_http(url: str, settings: dict) -> tuple[bytes, str, dict]:
    if not settings.get("allow_live_http"):
        fail("denied", "live HTTP is disabled (set allow_live_http)")
    host = (urlsplit(url).hostname or "").lower()
    if host not in {h.lower() for h in settings.get("allowed_hosts") or []}:
        fail("denied", "host is not allowlisted")
    request = urllib.request.Request(url, headers={"User-Agent": "knowledge-engine-news-fetcher/1"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310 - scheme checked by caller
        data = resp.read(MAX_BYTES + 1)
        media_type = (resp.headers.get_content_type() or "text/html").lower()
        final_url = resp.geturl()
    if len(data) > MAX_BYTES:
        fail("invalid_input", "page too large")
    meta = {
        "canonical_url": final_url,
        "rights": {"license": settings.get("default_license") or None, "retention": "retain"},
    }
    return data, media_type, meta


def main() -> None:
    if "--health" in sys.argv[1:]:
        json.dump({"ok": True}, sys.stdout)
        return
    request = json.load(sys.stdin)
    settings = request.get("settings") or {}
    url = request["input"]["url"]
    scheme = urlsplit(url).scheme.lower()
    if scheme == "file":
        data, media_type, meta = from_fixture(url, settings)
    elif scheme in {"http", "https"}:
        data, media_type, meta = from_http(url, settings)
    else:
        fail("invalid_input", "only file:// fixtures and http(s) URLs are supported")
    canonical = meta.get("canonical_url") or url
    rights = {"license": None, "retention": "retain", "notes": None, **(meta.get("rights") or {})}
    text = data.decode("utf-8", errors="replace")
    output = {
        "canonical_url": canonical,
        "title": meta.get("title"),
        "published_at": meta.get("published_at"),
        "retrieved_at": request["now"],
        "media_type": media_type,
        "byte_length": len(text.encode("utf-8")),
        "rights": rights,
    }
    artifact = {
        "media_type": media_type,
        "text": text,
        "canonical_uri": canonical,
        "title": meta.get("title"),
        "published_at": meta.get("published_at"),
        "retrieved_at": request["now"],
        "rights": rights,
    }
    json.dump({"output": output, "artifacts": [artifact]}, sys.stdout)


if __name__ == "__main__":
    main()
