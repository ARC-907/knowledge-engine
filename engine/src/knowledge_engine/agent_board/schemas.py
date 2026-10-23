"""Agent Board — communication schemas.

Channels, message types, visibility scopes, and per-type validators. Schemas
are intentionally permissive (`subject`/`body` accept free text) but enforce
a canonical taxonomy so cross-worktree posters stay legible.

Default channels mirror the listed coordination needs:

| Channel    | Purpose                                                      |
|------------|--------------------------------------------------------------|
| ops        | Engine ops — claims, releases, blockers, sweeper output      |
| research   | Cross-library research collaboration                         |
| project    | Project-level planning, status, decisions                    |
| worktree   | Per-worktree coordination across branches                    |
| branch     | Per-branch coordination across sessions                      |
| library    | Library-authoring research collaboration                     |
| planning   | High-level plan drafts, reviews, sign-offs                   |
| execution  | Build/run logs, deployment notes, ops checklists             |
| testing    | Test runs, regression triage, coverage discussions           |
| chatter    | Informal inter-agent chat (low signal, high churn)           |

The full set is dynamic — `board_config.channels_json` is the runtime source
of truth, this module just bakes in the defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_CHANNELS: tuple[str, ...] = (
    "ops",
    "research",
    "project",
    "worktree",
    "branch",
    "library",
    "planning",
    "execution",
    "testing",
    "chatter",
)


# Canonical message types. Posters can use anything in this set without
# board_config tweaks. Custom types are accepted (with a warning header in the
# response) so experimentation isn't blocked, but the dashboard groups unknown
# types under "other".
MESSAGE_TYPES: tuple[str, ...] = (
    # Lifecycle
    "claim",
    "release",
    "status_update",
    "blocker",
    "ack",
    "handoff_notice",
    "synthesis_ready",
    "human_attention_required",
    # Coordination
    "node_health",
    "policy_notice",
    "system_command",
    # Sweeper output
    "reminder",
    "digest",
    "tool_health_alert",
    # Research workflow
    "research_finding",
    "research_question",
    "citation_proposed",
    # Project workflow
    "plan_proposed",
    "plan_approved",
    "decision_recorded",
    "risk_flagged",
    # Worktree / branch coordination
    "branch_ready",
    "merge_proposed",
    "conflict_flagged",
    "rebase_recommended",
    # Library workflow
    "library_authoring_note",
    "library_review_requested",
    "library_published",
    # Execution / testing
    "build_started",
    "build_finished",
    "deploy_recorded",
    "test_run",
    "test_failure",
    "regression_triage",
    # Catch-all
    "chatter",
)


VISIBILITY_SCOPES: tuple[str, ...] = (
    "all",  # every worker sees it
    "task",  # only workers on same task_id
    "product",  # only workers on same product_id (library/chapter)
    "role",  # only workers with matching role
    "node",  # only the specified target node
)


@dataclass(frozen=True)
class MessageDraft:
    """Validated payload ready for `store.post_with_validation`."""

    channel: str
    message_type: str
    sender_node_id: str
    body: str
    sender_role: str | None = None
    task_id: str | None = None
    product_id: str | None = None
    subject: str | None = None
    visibility_scope: str = "all"
    target_node_id: str | None = None
    target_role: str | None = None
    requires_ack: bool = False
    reply_to: str | None = None
    correlation_id: str | None = None
    thread_id: str | None = None
    ttl_hours: int = 168
    model_id: str | None = None


# Per-field length caps. Body is intentionally generous (markdown bodies,
# stack traces, etc.) but every other field stays small enough that an
# attacker can't waste server time on huge payloads even before the
# request-size middleware fires.
MAX_LEN_BODY = 50_000
MAX_LEN_SUBJECT = 500
MAX_LEN_MESSAGE_TYPE = 64
MAX_LEN_CHANNEL = 64
MAX_LEN_SENDER_NODE_ID = 100
MAX_LEN_ROLE = 100
MAX_LEN_TASK_ID = 200
MAX_LEN_PRODUCT_ID = 200
MAX_LEN_CORRELATION_ID = 200
MAX_LEN_TARGET_NODE_ID = 200
MAX_LEN_REPLY_TO = 100
MAX_LEN_THREAD_ID = 200
MAX_LEN_MODEL_ID = 200

# Hard cap on TTL — one year. Keeps a misconfigured client from creating
# rows that effectively never expire.
MAX_TTL_HOURS = 24 * 365


@dataclass(frozen=True)
class FieldSpec:
    """One documented field of a board request body.

    The HTTP layer derives its OpenAPI request models from these tables; the
    runtime rules stay in :func:`validate` (and the caps above), which the
    models call, so there is one source of truth for what a message may hold.
    """

    name: str
    json_type: str  # "string" | "integer" | "boolean"
    description: str
    max_len: int | None = None
    required: bool = False
    default: Any = None
    enum: tuple[str, ...] | None = None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": self.json_type, "description": self.description}
        if self.max_len is not None:
            schema["maxLength"] = self.max_len
        if self.default is not None:
            schema["default"] = self.default
        if self.enum is not None:
            schema["enum"] = list(self.enum)
        if self.name == "ttl_hours":
            schema.update(minimum=0, maximum=MAX_TTL_HOURS)
        return schema


#: Fields of a message post (``POST /board/messages``), in documentation order.
MESSAGE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "channel",
        "string",
        "Channel; must be one of the board's configured channels.",
        MAX_LEN_CHANNEL,
        default="ops",
    ),
    FieldSpec(
        "message_type",
        "string",
        "Message type (canonical types in GET /board/message_types; custom types allowed).",
        MAX_LEN_MESSAGE_TYPE,
        required=True,
    ),
    FieldSpec(
        "sender_node_id",
        "string",
        "Identity of the posting agent/node.",
        MAX_LEN_SENDER_NODE_ID,
        required=True,
    ),
    FieldSpec(
        "body", "string", "Message body (free text / markdown).", MAX_LEN_BODY, required=True
    ),
    FieldSpec("subject", "string", "Optional one-line subject.", MAX_LEN_SUBJECT),
    FieldSpec(
        "visibility_scope",
        "string",
        "Who should see the message.",
        default="all",
        enum=VISIBILITY_SCOPES,
    ),
    FieldSpec("sender_role", "string", "Role of the sender.", MAX_LEN_ROLE),
    FieldSpec("task_id", "string", "Task the message belongs to.", MAX_LEN_TASK_ID),
    FieldSpec("product_id", "string", "Product (library/chapter) id.", MAX_LEN_PRODUCT_ID),
    FieldSpec(
        "target_node_id",
        "string",
        "Target node for visibility_scope=node.",
        MAX_LEN_TARGET_NODE_ID,
    ),
    FieldSpec("target_role", "string", "Target role for visibility_scope=role.", MAX_LEN_ROLE),
    FieldSpec("requires_ack", "boolean", "Ask recipients to acknowledge.", default=False),
    FieldSpec("reply_to", "string", "message_id this replies to.", MAX_LEN_REPLY_TO),
    FieldSpec("correlation_id", "string", "Thread correlation id.", MAX_LEN_CORRELATION_ID),
    FieldSpec("thread_id", "string", "Thread id.", MAX_LEN_THREAD_ID),
    FieldSpec(
        "ttl_hours",
        "integer",
        "Hours until the message expires (0 = never).",
        default=168,
    ),
    FieldSpec("model_id", "string", "Model that produced the message.", MAX_LEN_MODEL_ID),
)


def _check_len(errors: list[str], name: str, value: Any, cap: int) -> None:
    if value is None:
        return
    if len(str(value)) > cap:
        errors.append(f"{name} too long (max {cap} chars)")


def validate(
    draft: dict[str, Any],
    known_channels: list[str] | None = None,
    *,
    check_channel: bool = True,
) -> tuple[MessageDraft | None, list[str]]:
    """Validate a raw payload. Returns (draft, errors). On error, draft is None.

    `known_channels` defaults to DEFAULT_CHANNELS but is normally passed from
    `board_config.channels_json` so runtime additions are respected.
    `check_channel=False` skips only the channel-membership test (the HTTP
    request models use it: the configured set lives in the target scope's
    database and is checked again when the message is stored).
    """
    errors: list[str] = []
    channels = tuple(known_channels) if known_channels else DEFAULT_CHANNELS

    channel = str(draft.get("channel") or "ops").strip()
    if check_channel and channel not in channels:
        errors.append(f"channel '{channel}' not in configured set (known: {', '.join(channels)})")
    _check_len(errors, "channel", channel, MAX_LEN_CHANNEL)

    message_type = str(draft.get("message_type") or "").strip()
    if not message_type:
        errors.append("message_type is required")
    _check_len(errors, "message_type", message_type, MAX_LEN_MESSAGE_TYPE)

    sender_node_id = str(draft.get("sender_node_id") or "").strip()
    if not sender_node_id:
        errors.append("sender_node_id is required")
    _check_len(errors, "sender_node_id", sender_node_id, MAX_LEN_SENDER_NODE_ID)

    body = str(draft.get("body") or "").strip()
    if not body:
        errors.append("body is required")
    _check_len(errors, "body", body, MAX_LEN_BODY)

    subject = _opt_str(draft.get("subject"))
    _check_len(errors, "subject", subject, MAX_LEN_SUBJECT)

    visibility = str(draft.get("visibility_scope") or "all").strip()
    if visibility not in VISIBILITY_SCOPES:
        errors.append(f"visibility_scope '{visibility}' not in {VISIBILITY_SCOPES}")

    # Per-field length caps for the remaining identifier fields.
    sender_role = _opt_str(draft.get("sender_role"))
    task_id = _opt_str(draft.get("task_id"))
    product_id = _opt_str(draft.get("product_id"))
    target_node_id = _opt_str(draft.get("target_node_id"))
    target_role = _opt_str(draft.get("target_role"))
    reply_to = _opt_str(draft.get("reply_to"))
    correlation_id = _opt_str(draft.get("correlation_id"))
    thread_id = _opt_str(draft.get("thread_id"))
    model_id = _opt_str(draft.get("model_id"))
    _check_len(errors, "sender_role", sender_role, MAX_LEN_ROLE)
    _check_len(errors, "task_id", task_id, MAX_LEN_TASK_ID)
    _check_len(errors, "product_id", product_id, MAX_LEN_PRODUCT_ID)
    _check_len(errors, "target_node_id", target_node_id, MAX_LEN_TARGET_NODE_ID)
    _check_len(errors, "target_role", target_role, MAX_LEN_ROLE)
    _check_len(errors, "reply_to", reply_to, MAX_LEN_REPLY_TO)
    _check_len(errors, "correlation_id", correlation_id, MAX_LEN_CORRELATION_ID)
    _check_len(errors, "thread_id", thread_id, MAX_LEN_THREAD_ID)
    _check_len(errors, "model_id", model_id, MAX_LEN_MODEL_ID)

    ttl_raw = draft.get("ttl_hours", 168)
    try:
        ttl_hours = int(ttl_raw)
    except (TypeError, ValueError):
        errors.append("ttl_hours must be an integer")
        ttl_hours = 168
    if ttl_hours < 0:
        errors.append("ttl_hours must be >= 0 (0 = no expiry)")
    if ttl_hours > MAX_TTL_HOURS:
        errors.append(f"ttl_hours too large (max {MAX_TTL_HOURS} = one year)")

    if errors:
        return None, errors

    return (
        MessageDraft(
            channel=channel,
            message_type=message_type,
            sender_node_id=sender_node_id,
            body=body,
            sender_role=sender_role,
            task_id=task_id,
            product_id=product_id,
            subject=subject,
            visibility_scope=visibility,
            target_node_id=target_node_id,
            target_role=target_role,
            requires_ack=bool(draft.get("requires_ack", False)),
            reply_to=reply_to,
            correlation_id=correlation_id,
            thread_id=thread_id,
            ttl_hours=ttl_hours,
            model_id=model_id,
        ),
        [],
    )


def is_known_type(message_type: str) -> bool:
    return message_type in MESSAGE_TYPES


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
