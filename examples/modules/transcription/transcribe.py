"""Reference transcription module (script transport, protocol 1, long-running).

Input: ``{"artifact_id": ..., "language": ...}``. The host resolves the
artifact id (only artifacts approved for the call's scope) and passes its
read-only file in ``request["artifacts"][artifact_id]["path"]``.

Engines:

* ``fake`` (default) -- deterministic and offline: every non-empty line of a
  text stand-in (``Speaker: words``) becomes one segment of 400 ms per word;
* ``command`` -- an operator-installed CLI (``settings.command``, argv with
  ``{input}``), run without a shell; it must print
  ``{"language", "segments": [{"start_ms", "end_ms", "speaker", "text"}],
  "engine": {"name", "version", "model"}}``. It runs in the module's process
  group, so a host cancellation or timeout stops it too.

Standard library only.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

WORD_MS = 400
FAKE_ENGINE = {"name": "fake-transcriber", "version": "1.0.0", "model": "deterministic-words-v1"}


def fail(code: str, message: str) -> None:
    json.dump({"error": {"code": code, "message": message}}, sys.stdout)
    sys.exit(0)


def fake(path: Path, language: str, delay_ms: int) -> dict:
    if delay_ms > 0:
        time.sleep(delay_ms / 1000)
    segments = []
    clock = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        speaker, sep, text = line.partition(":")
        if not sep:
            speaker, text = "", line
        text = text.strip()
        duration = max(1, len(text.split())) * WORD_MS
        segments.append(
            {"start_ms": clock, "end_ms": clock + duration, "speaker": speaker.strip() or None, "text": text}
        )
        clock += duration
    return {"language": language, "segments": segments, "engine": FAKE_ENGINE}


def external(path: Path, argv_template: list[str]) -> dict:
    if not argv_template:
        fail("unavailable", "engine=command needs the 'command' setting")
    argv = [part.replace("{input}", str(path)) for part in argv_template]
    try:
        done = subprocess.run(argv, capture_output=True, check=False)  # noqa: S603 - operator argv, no shell
    except OSError:
        fail("unavailable", "the configured transcription engine cannot be started")
    if done.returncode != 0:
        fail("unavailable", f"the transcription engine exited with status {done.returncode}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        fail("output_invalid", "the transcription engine did not print JSON")
    return {}


def main() -> None:
    if "--health" in sys.argv[1:]:
        json.dump({"ok": True}, sys.stdout)
        return
    request = json.load(sys.stdin)
    settings = request.get("settings") or {}
    artifact_id = request["input"]["artifact_id"]
    parent = (request.get("artifacts") or {}).get(artifact_id)
    if parent is None:
        fail("invalid_input", "the artifact was not provided by the host")
    language = request["input"].get("language") or settings.get("default_language") or "en"
    path = Path(parent["path"])
    if settings.get("engine", "fake") == "command":
        result = external(path, list(settings.get("command") or []))
    else:
        result = fake(path, language, int(settings.get("fake_delay_ms") or 0))
    output = {
        "language": result.get("language") or language,
        "segments": result.get("segments") or [],
        "engine": result.get("engine") or {"name": "unknown", "version": "unknown", "model": "unknown"},
        "parent": {
            "artifact_id": artifact_id,
            "digest": parent["digest"],
            "canonical_uri": parent.get("canonical_uri"),
        },
    }
    artifact = {
        "media_type": "application/json",
        "text": json.dumps(output, sort_keys=True),
        "record_key": "transcript-" + parent["digest"].removeprefix("sha256:")[:32],
        "canonical_uri": parent.get("canonical_uri"),
        "title": "Transcript",
        "language": output["language"],
        "parent_artifact_ids": [artifact_id],
        "metadata": {"engine": output["engine"], "segments": len(output["segments"])},
    }
    json.dump({"output": output, "artifacts": [artifact]}, sys.stdout)


if __name__ == "__main__":
    main()
