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
    "claim", "release", "status_update", "blocker", "ack",
    "handoff_notice", "synthesis_ready", "human_attention_required",
    # Coordination
    "node_health", "policy_notice", "system_command",
    # Sweeper output
    "reminder", "digest", "tool_health_alert",
    # Research workflow
    "research_finding", "research_question", "citation_proposed",
    # Project workflow
    "plan_proposed", "plan_approved", "decision_recorded", "risk_flagged",
    # Worktree / branch coordination
    "branch_ready", "merge_proposed", "conflict_flagged", "rebase_recommended",
    # Library workflow
    "library_authoring_note", "library_review_requested", "library_published",
    # Execution / testing
    "build_started", "build_finished", "deploy_recorded",
    "test_run", "test_failure", "regression_triage",
    # Catch-all
    "chatter",
)


VISIBILITY_SCOPES: tuple[str, ...] = (
    "all",      # every worker sees it
    "task",     # only workers on same task_id
    "product",  # only workers on same product_id (library/chapter)
    "role",     # only workers with matching role
    "node",     # only the specified target node
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


def validate(draft: dict[str, Any], known_channels: list[str] | None = None) -> tuple[MessageDraft | None, list[str]]:
    """Validate a raw payload. Returns (draft, errors). On error, draft is None.

    `known_channels` defaults to DEFAULT_CHANNELS but is normally passed from
    `board_config.channels_json` so runtime additions are respected.
    """
    errors: list[str] = []
    channels = tuple(known_channels) if known_channels else DEFAULT_CHANNELS

    channel = str(draft.get("channel") or "ops").strip()
    if channel not in channels:
        errors.append(
            f"channel '{channel}' not in configured set "
            f"(known: {', '.join(channels)})"
        )

    message_type = str(draft.get("message_type") or "").strip()
    if not message_type:
        errors.append("message_type is required")

    sender_node_id = str(draft.get("sender_node_id") or "").strip()
    if not sender_node_id:
        errors.append("sender_node_id is required")
    if len(sender_node_id) > 100:
        errors.append("sender_node_id too long (max 100 chars)")

    body = str(draft.get("body") or "").strip()
    if not body:
        errors.append("body is required")
    if len(body) > 50000:
        errors.append("body too long (max 50000 chars)")

    visibility = str(draft.get("visibility_scope") or "all").strip()
    if visibility not in VISIBILITY_SCOPES:
        errors.append(
            f"visibility_scope '{visibility}' not in {VISIBILITY_SCOPES}"
        )

    ttl_raw = draft.get("ttl_hours", 168)
    try:
        ttl_hours = int(ttl_raw)
    except (TypeError, ValueError):
        errors.append("ttl_hours must be an integer")
        ttl_hours = 168
    if ttl_hours < 0:
        errors.append("ttl_hours must be >= 0 (0 = no expiry)")

    if errors:
        return None, errors

    return (
        MessageDraft(
            channel=channel,
            message_type=message_type,
            sender_node_id=sender_node_id,
            body=body,
            sender_role=_opt_str(draft.get("sender_role")),
            task_id=_opt_str(draft.get("task_id")),
            product_id=_opt_str(draft.get("product_id")),
            subject=_opt_str(draft.get("subject")),
            visibility_scope=visibility,
            target_node_id=_opt_str(draft.get("target_node_id")),
            target_role=_opt_str(draft.get("target_role")),
            requires_ack=bool(draft.get("requires_ack", False)),
            reply_to=_opt_str(draft.get("reply_to")),
            correlation_id=_opt_str(draft.get("correlation_id")),
            thread_id=_opt_str(draft.get("thread_id")),
            ttl_hours=ttl_hours,
            model_id=_opt_str(draft.get("model_id")),
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
