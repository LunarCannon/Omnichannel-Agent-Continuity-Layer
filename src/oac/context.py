from __future__ import annotations

from pathlib import Path

from .identity import resolve_identity_alias
from .synthesize import events_for_user, load_jsonl, load_state, synthesize_digest

TRUNCATION_MARKER = "[truncated to fit max chars]\n"


def build_context_brief(
    *,
    store: Path,
    surface: str,
    canonical_user_id: str = "",
    channel_id: str = "",
    sender: str = "",
    query: str = "",
    as_of_ms: int | None = None,
    max_chars: int = 1800,
    max_events: int = 12,
) -> str:
    events_path = store / "events.jsonl"
    state_path = store / "state.json"
    if not events_path.exists():
        return ""

    events = load_jsonl(events_path)
    if not events:
        return ""

    state = load_state(state_path)
    resolved_user_id = canonical_user_id
    if not resolved_user_id and channel_id and sender:
        resolved_user_id = resolve_identity_alias(
            state=state,
            surface=surface,
            channel_id=channel_id,
            sender=sender,
        )
    if not resolved_user_id:
        return ""
    if not events_for_user(events, resolved_user_id):
        return ""

    digest = synthesize_digest(
        events=events,
        state=state,
        surface=surface,
        canonical_user_id=resolved_user_id,
        query=query,
        as_of_ms=as_of_ms,
        max_events=max_events,
    )
    return truncate_context(str(digest["markdown"]), max_chars=max_chars)


def truncate_context(markdown: str, *, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(markdown) <= max_chars:
        return markdown
    if max_chars <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:max_chars]

    budget = max_chars - len(TRUNCATION_MARKER)
    prefix = markdown[:budget]
    if "\n" in prefix:
        prefix = prefix[: prefix.rstrip("\n").rfind("\n") + 1]
    else:
        prefix = ""
    return prefix + TRUNCATION_MARKER
