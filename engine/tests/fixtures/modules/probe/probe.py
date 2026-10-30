"""Test fixture module: one script, behaviour chosen by ``input.mode``."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def out(payload):
    json.dump(payload, sys.stdout)
    sys.stdout.flush()


def main():
    if "--health" in sys.argv[1:]:
        sys.exit(int(os.environ.get("PROBE_HEALTH_EXIT", "0")))
    request = json.load(sys.stdin)
    args = request["input"]
    mode = args.get("mode", "echo")
    token = os.environ.get("PROBE_TOKEN", "")
    if mode == "echo":
        out(
            {
                "output": {
                    "mode": mode,
                    "input": args,
                    "scope": request["scope"],
                    "tool": request["tool"],
                }
            }
        )
    elif mode == "sleep":
        time.sleep(float(args.get("seconds", 30)))
        out({"output": {"mode": mode}})
    elif mode == "spawn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        Path(args["pid_file"]).write_text(str(child.pid))
        time.sleep(float(args.get("seconds", 120)))
        out({"output": {"mode": mode}})
    elif mode == "big":
        out({"output": {"mode": mode, "blob": "x" * 100000}})
    elif mode == "bad_output":
        out({"output": {"nope": True}})
    elif mode == "garbage":
        sys.stdout.write("not json")
    elif mode == "fail_once":
        marker = Path(args["marker"])
        count = int(marker.read_text()) if marker.exists() else 0
        marker.write_text(str(count + 1))
        if count == 0:
            sys.exit(3)
        out({"output": {"mode": mode, "attempt": count + 1}})
    elif mode == "secret":
        sys.stderr.write("token=" + token + "\n")
        out({"output": {"mode": mode, "leak": "value " + token}})
    elif mode == "secret_error":
        out({"error": {"code": "unavailable", "message": "upstream rejected " + token}})
    elif mode == "secret_artifact":
        out({"output": {"mode": mode}, "artifacts": [{"media_type": "text/plain", "text": token}]})
    elif mode == "artifacts":
        count = int(args.get("count", 1))
        size = int(args.get("size", 10))
        out(
            {
                "output": {"mode": mode},
                "artifacts": [
                    {"media_type": "text/plain", "text": str(i) * size, "record_key": f"rec-{i}"}
                    for i in range(count)
                ],
            }
        )
    elif mode == "module_error":
        out({"error": {"code": "invalid_input", "message": "the module did not like that"}})
    else:
        out({"output": {"mode": mode}})


main()
