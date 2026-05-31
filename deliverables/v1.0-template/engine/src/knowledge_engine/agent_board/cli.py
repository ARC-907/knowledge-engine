"""Agent Board — CLI client.

`knowledge-engine board ...` subcommand handler. Targets the engine HTTP API
(default `http://127.0.0.1:9210`) and prints JSON or human output.

Covers the full board surface: `status`, `read`, `post`, `thread`, `search`,
`digest`, `ack`, `sweep`, plus `keys` and `config` sub-groups. Body input
accepts a literal string, `@/path/to/file`, or `-` for stdin so agents can
pipe long check-ins without quoting headaches.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Windows consoles default to cp1252; em-dashes, arrows, and emoji in agent
# bodies otherwise raise UnicodeEncodeError on a read pass.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def _base_url(arg_url: str | None) -> str:
    if arg_url:
        return arg_url.rstrip("/")
    env_url = os.environ.get("KE_BOARD_URL")
    if env_url:
        return env_url.rstrip("/")
    port = os.environ.get("KE_BOARD_PORT", os.environ.get("KE_PORT", "9210"))
    return f"http://127.0.0.1:{port}"


def _resolve_body(raw: str) -> str:
    if raw == "-":
        return sys.stdin.read().strip()
    if raw.startswith("@"):
        with open(raw[1:], "r", encoding="utf-8") as f:
            return f.read().strip()
    return raw


def _get(url: str, key: str | None = None) -> Any:
    req = urllib.request.Request(url, method="GET")
    if key:
        req.add_header("X-Board-Key", key)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(url: str, payload: dict[str, Any], key: str | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if key:
        req.add_header("X-Board-Key", key)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _patch(url: str, payload: dict[str, Any], key: str | None = None) -> Any:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="PATCH",
        headers={"Content-Type": "application/json"},
    )
    if key:
        req.add_header("X-Board-Key", key)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _delete(url: str, key: str | None = None) -> Any:
    req = urllib.request.Request(url, method="DELETE")
    if key:
        req.add_header("X-Board-Key", key)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body) if body else {"ok": True}


def _print_messages(msgs: list[dict[str, Any]]) -> None:
    if not msgs:
        print("(no messages)")
        return
    for m in msgs:
        when = m.get("created_at") or m.get("time") or ""
        ch = m.get("channel") or "?"
        mt = m.get("message_type") or "?"
        who = m.get("sender_node_id") or m.get("from") or "?"
        mid = m.get("message_id", "")[:8]
        subj = m.get("subject") or ""
        print(f"\n--- [{mid}] {when}  ch:{ch} type:{mt} from:{who}")
        if subj:
            print(f"# {subj}")
        body = m.get("body") or ""
        if body:
            print(body)


# ── Subcommand handlers ────────────────────────────────────────


def _scope_of(args) -> str | None:
    """The --scope value, normalized to None when absent/blank."""
    s = (getattr(args, "scope", None) or "").strip()
    return s or None


def cmd_status(args, base: str) -> int:
    try:
        st = _get(f"{base}/board/status")
    except urllib.error.URLError as e:
        print(f"board unreachable at {base}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(st, indent=2))
    return 0


def cmd_channels(args, base: str) -> int:
    print(json.dumps(_get(f"{base}/board/channels"), indent=2))
    return 0


def cmd_types(args, base: str) -> int:
    print(json.dumps(_get(f"{base}/board/message_types"), indent=2))
    return 0


def cmd_read(args, base: str) -> int:
    params: dict[str, str] = {}
    if args.since:
        params["since"] = args.since
    if args.channel:
        params["channel"] = args.channel
    if args.message_type:
        params["message_type"] = args.message_type
    if args.task_id:
        params["task_id"] = args.task_id
    if args.product_id:
        params["product_id"] = args.product_id
    if args.sender:
        params["sender_node_id"] = args.sender
    if _scope_of(args):
        params["scope"] = _scope_of(args)
    params["limit"] = str(args.limit)
    qs = urllib.parse.urlencode(params)
    url = f"{base}/board/messages?{qs}"
    try:
        msgs = _get(url)
    except urllib.error.URLError as e:
        print(f"board unreachable at {base}: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(msgs, indent=2))
    else:
        _print_messages(msgs)
    return 0


def cmd_post(args, base: str) -> int:
    try:
        body = _resolve_body(args.body)
    except OSError as e:
        print(f"could not read body: {e}", file=sys.stderr)
        return 1
    payload = {
        "channel": args.channel,
        "message_type": args.message_type,
        "sender_node_id": args.sender,
        "body": body,
    }
    if args.subject:
        payload["subject"] = args.subject
    if args.task_id:
        payload["task_id"] = args.task_id
    if args.product_id:
        payload["product_id"] = args.product_id
    if args.role:
        payload["sender_role"] = args.role
    if args.visibility:
        payload["visibility_scope"] = args.visibility
    if args.requires_ack:
        payload["requires_ack"] = True
    if args.reply_to:
        payload["reply_to"] = args.reply_to
    if args.correlation_id:
        payload["correlation_id"] = args.correlation_id
    if args.ttl_hours is not None:
        payload["ttl_hours"] = args.ttl_hours
    if _scope_of(args):
        payload["scope"] = _scope_of(args)
    try:
        msg = _post(f"{base}/board/messages", payload, key=args.key)
    except urllib.error.HTTPError as e:
        print(f"post rejected ({e.code}): {e.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"board unreachable at {base}: {e}", file=sys.stderr)
        return 2
    print(json.dumps(msg, indent=2))
    return 0


def cmd_thread(args, base: str) -> int:
    ident = args.correlation_id or args.thread_id
    if not ident:
        print(
            "thread requires either a positional correlation_id or --thread-id",
            file=sys.stderr,
        )
        return 1
    quoted = urllib.parse.quote(ident, safe="")
    # The route is /board/threads/{correlation_id}. The store also accepts
    # thread_id — when the caller passes --thread-id we route through the
    # threads endpoint as a correlation_id; the store layer falls back to
    # thread_id when correlation_id misses.
    params: dict[str, str] = {}
    if args.limit:
        params["limit"] = str(args.limit)
    if _scope_of(args):
        params["scope"] = _scope_of(args)
    url = f"{base}/board/threads/{quoted}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    msgs = _get(url)
    if args.json:
        print(json.dumps(msgs, indent=2))
    else:
        _print_messages(msgs)
    return 0


def cmd_search(args, base: str) -> int:
    params = {"q": args.query, "limit": str(args.limit)}
    if args.channel:
        params["channel"] = args.channel
    if _scope_of(args):
        params["scope"] = _scope_of(args)
    qs = urllib.parse.urlencode(params)
    hits = _get(f"{base}/board/search?{qs}")
    print(json.dumps(hits, indent=2))
    return 0


def cmd_digest(args, base: str) -> int:
    params: dict[str, str] = {}
    if args.channel:
        params["channel"] = args.channel
    if args.since:
        params["since"] = args.since
    if args.max_messages:
        params["max_messages"] = str(args.max_messages)
    if _scope_of(args):
        params["scope"] = _scope_of(args)
    qs = urllib.parse.urlencode(params)
    url = f"{base}/board/digest" + (f"?{qs}" if qs else "")
    print(json.dumps(_get(url), indent=2))
    return 0


def cmd_scopes(args, base: str) -> int:
    print(json.dumps(_get(f"{base}/board/scopes"), indent=2))
    return 0


def cmd_ack(args, base: str) -> int:
    payload = {"from": args.sender}
    if _scope_of(args):
        payload["scope"] = _scope_of(args)
    try:
        msg = _post(
            f"{base}/board/messages/{urllib.parse.quote(args.message_id, safe='')}/ack",
            payload, key=args.key,
        )
    except urllib.error.HTTPError as e:
        print(f"ack rejected ({e.code}): {e.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    print(json.dumps(msg, indent=2))
    return 0


def cmd_sweep(args, base: str) -> int:
    try:
        result = _post(f"{base}/board/sweep", {}, key=args.key)
    except urllib.error.HTTPError as e:
        print(f"sweep rejected ({e.code}): {e.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_keys_list(args, base: str) -> int:
    print(json.dumps(_get(f"{base}/board/keys", key=args.key), indent=2))
    return 0


def cmd_keys_create(args, base: str) -> int:
    payload: dict[str, Any] = {"display_name": args.name}
    if args.notes:
        payload["notes"] = args.notes
    if args.permission:
        payload["permissions"] = [{
            "resource_type": args.permission_resource_type,
            "resource_id": args.permission_resource_id,
            "permission": args.permission,
        }]
    print(json.dumps(_post(f"{base}/board/keys", payload, key=args.key), indent=2))
    return 0


def cmd_keys_revoke(args, base: str) -> int:
    print(json.dumps(
        _delete(f"{base}/board/keys/{urllib.parse.quote(args.key_id, safe='')}",
                key=args.key),
        indent=2,
    ))
    return 0


def cmd_keys_bootstrap(args, base: str) -> int:
    try:
        result = _post(f"{base}/board/keys/bootstrap-master", {})
    except urllib.error.HTTPError as e:
        print(f"bootstrap rejected ({e.code}): {e.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_config_get(args, base: str) -> int:
    print(json.dumps(_get(f"{base}/board/config"), indent=2))
    return 0


def cmd_config_set(args, base: str) -> int:
    patch: dict[str, Any] = {}
    if args.engine_port is not None:
        patch["engine_port"] = args.engine_port
    if args.standalone_port is not None:
        patch["standalone_port"] = args.standalone_port
    if args.sweep_interval_s is not None:
        patch["sweep_interval_s"] = args.sweep_interval_s
    if args.stale_blocker_hours is not None:
        patch["stale_blocker_hours"] = args.stale_blocker_hours
    if args.digest_interval_minutes is not None:
        patch["digest_interval_minutes"] = args.digest_interval_minutes
    if args.default_ttl_hours is not None:
        patch["default_ttl_hours"] = args.default_ttl_hours
    if args.max_messages is not None:
        patch["max_messages_before_prune"] = args.max_messages
    if args.sweeper is not None:
        patch["sweeper_enabled"] = 1 if args.sweeper == "on" else 0
    if args.require_key is not None:
        patch["require_key_for_post"] = 1 if args.require_key == "on" else 0
    if not patch:
        print("nothing to patch", file=sys.stderr)
        return 1
    print(json.dumps(_patch(f"{base}/board/config", patch, key=args.key), indent=2))
    return 0


# ── Argparse ───────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="knowledge-engine board",
        description="Agent Board CLI — post, read, search, ack, sweep, manage keys.",
    )
    p.add_argument("--url", help="Board base URL (default $KE_BOARD_URL or http://127.0.0.1:9210).")
    p.add_argument("--key", help="X-Board-Key for gated endpoints (or $KE_BOARD_KEY).")

    # Shared --scope flag for the data subcommands. A scope routes the
    # request to a per-project / per-branch / per-agent physical database;
    # omit it to use the shared board. Added as a parent parser so it can
    # appear after the verb (`board read --scope proj-a`).
    scope_parent = argparse.ArgumentParser(add_help=False)
    scope_parent.add_argument(
        "--scope",
        help="Isolation key (project/branch/agent/loop) — routes to that scope's DB.",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Service health + counts + last-sweep.").set_defaults(func=cmd_status)
    sub.add_parser("channels", help="List configured channels.").set_defaults(func=cmd_channels)
    sub.add_parser("types", help="List canonical message types + scopes.").set_defaults(func=cmd_types)
    sub.add_parser("scopes", help="List per-scope databases.").set_defaults(func=cmd_scopes)

    pr = sub.add_parser("read", help="Read messages.", parents=[scope_parent])
    pr.add_argument("--since")
    pr.add_argument("--channel")
    pr.add_argument("--type", dest="message_type")
    # `--task` and `--task-id` are the same flag — both populate `task_id`.
    # Same for `--product` / `--product-id`. Avoids the classic
    # "I typed --task and nothing filtered" papercut.
    pr.add_argument("--task", "--task-id", dest="task_id")
    pr.add_argument("--product", "--product-id", dest="product_id")
    pr.add_argument("--sender")
    pr.add_argument("--limit", type=int, default=20)
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_read)

    pp = sub.add_parser("post", help="Post a message.", parents=[scope_parent])
    pp.add_argument("--from", dest="sender", required=True)
    pp.add_argument("--channel", default="ops")
    pp.add_argument("--type", dest="message_type", required=True)
    pp.add_argument("--subject")
    pp.add_argument("--body", required=True, help="Text body, @path, or - for stdin.")
    pp.add_argument("--task-id")
    pp.add_argument("--product-id")
    pp.add_argument("--role")
    pp.add_argument("--visibility")
    pp.add_argument("--requires-ack", action="store_true")
    pp.add_argument("--reply-to")
    pp.add_argument("--correlation-id")
    pp.add_argument("--ttl-hours", type=int)
    pp.set_defaults(func=cmd_post)

    pt = sub.add_parser(
        "thread",
        help="Fetch a thread by correlation_id (positional) or --thread-id.",
        parents=[scope_parent],
    )
    # Both surfaces work — the route accepts either. `correlation_id` stays
    # positional for muscle memory; `--thread-id` is here so scripts using
    # the long-lived thread id don't have to invent a fake correlation.
    pt.add_argument("correlation_id", nargs="?")
    pt.add_argument("--thread-id", dest="thread_id")
    pt.add_argument("--limit", type=int)
    pt.add_argument("--json", action="store_true")
    pt.set_defaults(func=cmd_thread)

    ps = sub.add_parser("search", help="FTS5 search.", parents=[scope_parent])
    ps.add_argument("query")
    ps.add_argument("--channel")
    ps.add_argument("--limit", type=int, default=25)
    ps.set_defaults(func=cmd_search)

    pd = sub.add_parser("digest", help="Context-compressed summary.", parents=[scope_parent])
    pd.add_argument("--channel")
    pd.add_argument("--since")
    pd.add_argument("--max-messages", type=int)
    pd.set_defaults(func=cmd_digest)

    pa = sub.add_parser("ack", help="Acknowledge a message.", parents=[scope_parent])
    pa.add_argument("message_id")
    pa.add_argument("--from", dest="sender", required=True)
    pa.set_defaults(func=cmd_ack)

    sub.add_parser("sweep", help="Run a sweeper pass now.").set_defaults(func=cmd_sweep)

    pk = sub.add_parser("keys", help="Provider-key management.")
    pks = pk.add_subparsers(dest="keys_cmd", required=True)
    pks.add_parser("list").set_defaults(func=cmd_keys_list)
    pkc = pks.add_parser("create")
    pkc.add_argument("name")
    pkc.add_argument("--notes")
    pkc.add_argument("--permission", choices=["read", "write", "invoke", "admin"])
    pkc.add_argument("--permission-resource-type", default="*")
    pkc.add_argument("--permission-resource-id", default="*")
    pkc.set_defaults(func=cmd_keys_create)
    pkr = pks.add_parser("revoke")
    pkr.add_argument("key_id")
    pkr.set_defaults(func=cmd_keys_revoke)
    pks.add_parser(
        "bootstrap-master",
        help="One-shot master-key creation (localhost only).",
    ).set_defaults(func=cmd_keys_bootstrap)

    pc = sub.add_parser("config", help="Board configuration.")
    pcs = pc.add_subparsers(dest="config_cmd", required=True)
    pcs.add_parser("show").set_defaults(func=cmd_config_get)
    pcset = pcs.add_parser("set")
    pcset.add_argument("--engine-port", type=int)
    pcset.add_argument("--standalone-port", type=int)
    pcset.add_argument("--sweep-interval-s", type=int)
    pcset.add_argument("--stale-blocker-hours", type=int)
    pcset.add_argument("--digest-interval-minutes", type=int)
    pcset.add_argument("--default-ttl-hours", type=int)
    pcset.add_argument("--max-messages", type=int)
    pcset.add_argument("--sweeper", choices=["on", "off"])
    pcset.add_argument("--require-key", choices=["on", "off"])
    pcset.set_defaults(func=cmd_config_set)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "key", None):
        args.key = os.environ.get("KE_BOARD_KEY")
    base = _base_url(args.url)
    try:
        return args.func(args, base)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"board unreachable at {base}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
