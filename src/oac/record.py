from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterator

from .synthesize import DEFAULT_SURFACE_POLICIES, atomic_write_json, load_state, topic_state_key

VALID_ROLES = {"user", "assistant", "system", "tool"}
VALID_SENSITIVITIES = {"public", "private", "sensitive", "secret"}
SECRET_KEY_RE = re.compile(r"(secret|token|password|pass|vault|code|api[_ -]?key|credential)", re.IGNORECASE)
SECRET_PHRASE_RE = re.compile(
    r"\b((?:vault\s+)?(?:code|secret|token|password|api\s+key|credential)(?:\s+(?:is|=|:))\s+)([A-Za-z0-9_:/+=-]{3,})",
    re.IGNORECASE,
)


def record_event(
    *,
    store: Path,
    surface: str,
    channel_id: str,
    sender: str,
    canonical_user_id: str,
    role: str,
    summary: str,
    topic_id: str,
    sensitivity: str = "private",
    timestamp_ms: int | None = None,
    event_id: str | None = None,
    topic_title: str | None = None,
    continuity_intent: str = "continue_topic",
    modality: str = "text",
    artifact_ref: str | None = None,
    decisions: list[str] | None = None,
    questions: list[str] | None = None,
    promises: list[str] | None = None,
    facts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    timestamp = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    decisions = [redact_text(item) for item in decisions or []]
    questions = [redact_text(item) for item in questions or []]
    promises = [redact_text(item) for item in promises or []]
    facts = [redact_fact(fact) for fact in facts or []]
    event = {
        "id": event_id
        or deterministic_event_id(
            timestamp_ms=timestamp,
            surface=surface,
            channel_id=channel_id,
            sender=sender,
            canonical_user_id=canonical_user_id,
            role=role,
            summary=redact_text(summary),
            topic_id=topic_id,
        ),
        "timestamp_ms": timestamp,
        "surface": surface,
        "channel_id": channel_id,
        "sender": sender,
        "canonical_user_id": canonical_user_id,
        "role": role,
        "summary": redact_text(summary),
        "sensitivity": sensitivity,
        "topic_id": topic_id,
        "continuity_intent": continuity_intent,
        "modality": modality,
    }
    if artifact_ref:
        event["artifact_ref"] = artifact_ref
    if decisions:
        event["decisions"] = decisions
    if questions:
        event["questions"] = questions
    if promises:
        event["promises"] = promises
    if facts:
        event["facts"] = facts

    validate_event(event)

    store.mkdir(parents=True, exist_ok=True)
    events_path = store / "events.jsonl"
    state_path = store / "state.json"
    lock_path = store / ".lock"
    with file_lock(lock_path):
        append_jsonl(events_path, event)
        state = migrate_state(load_state(state_path))
        update_state(state, event, topic_title=topic_title)
        atomic_write_json(state_path, state)
    return event


def deterministic_event_id(
    *,
    timestamp_ms: int,
    surface: str,
    channel_id: str,
    sender: str,
    canonical_user_id: str,
    role: str,
    summary: str,
    topic_id: str,
) -> str:
    payload = {
        "timestamp_ms": timestamp_ms,
        "surface": surface,
        "channel_id": channel_id,
        "sender": sender,
        "canonical_user_id": canonical_user_id,
        "role": role,
        "summary": summary,
        "topic_id": topic_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"evt_{hashlib.sha256(encoded).hexdigest()[:16]}"


def parse_fact(value: str) -> dict[str, str]:
    if "=" not in value:
        raise ValueError(f"Invalid --fact {value!r}: expected KEY=VALUE")
    key, fact_value = value.split("=", 1)
    key = key.strip()
    if not key:
        raise ValueError(f"Invalid --fact {value!r}: key cannot be empty")
    return {"key": key, "value": fact_value.strip()}


def redact_fact(fact: dict[str, str]) -> dict[str, str]:
    key = str(fact.get("key", ""))
    value = str(fact.get("value", ""))
    if SECRET_KEY_RE.search(key):
        value = "[REDACTED]"
    else:
        value = redact_text(value)
    return {"key": key, "value": value}


def redact_text(value: str) -> str:
    return SECRET_PHRASE_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)


def validate_event(event: dict[str, Any]) -> None:
    required = [
        "id",
        "timestamp_ms",
        "surface",
        "channel_id",
        "sender",
        "canonical_user_id",
        "role",
        "summary",
        "sensitivity",
        "topic_id",
        "continuity_intent",
        "modality",
    ]
    missing = [field for field in required if event.get(field) in (None, "")]
    if missing:
        raise ValueError(f"Missing required event fields: {', '.join(missing)}")
    if event["role"] not in VALID_ROLES:
        raise ValueError(f"Invalid role {event['role']!r}; expected one of {sorted(VALID_ROLES)}")
    if event["sensitivity"] not in VALID_SENSITIVITIES:
        raise ValueError(
            f"Invalid sensitivity {event['sensitivity']!r}; expected one of {sorted(VALID_SENSITIVITIES)}"
        )
    for field in ("id", "surface", "channel_id", "sender", "canonical_user_id", "topic_id", "modality"):
        validate_no_control_chars(field, str(event[field]))
    if event.get("artifact_ref") is not None:
        validate_no_control_chars("artifact_ref", str(event["artifact_ref"]))
    int(event["timestamp_ms"])


def validate_no_control_chars(name: str, value: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Event {name} cannot contain control characters")


def migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    state.setdefault("version", 1)
    state.setdefault("current_focus", [])
    state.setdefault("open_questions", [])
    state.setdefault("recent_decisions", [])
    state.setdefault("pending_promises", [])
    state.setdefault("tasks", {})
    state.setdefault("surface_policies", DEFAULT_SURFACE_POLICIES)
    state.setdefault("identity_aliases", {})
    state.setdefault("topics", {})
    state.setdefault("active_topic_ids", [])
    return state


def update_state(state: dict[str, Any], event: dict[str, Any], *, topic_title: str | None = None) -> None:
    topic_id = str(event["topic_id"])
    canonical_user_id = str(event["canonical_user_id"])
    topics = state.setdefault("topics", {})
    storage_key = topic_state_key(canonical_user_id, topic_id)
    previous_topic = topics.get(storage_key, {}) if isinstance(topics.get(storage_key), dict) else {}
    topics[storage_key] = {
        "canonical_user_id": canonical_user_id,
        "topic_id": topic_id,
        "title": topic_title or previous_topic.get("title") or topic_id,
        "summary": event["summary"],
        "last_event_id": event["id"],
        "last_updated_ms": event["timestamp_ms"],
    }

    active_topic_ids = [item for item in state.get("active_topic_ids", []) if item != topic_id]
    active_topic_ids.append(topic_id)
    state["active_topic_ids"] = active_topic_ids[-12:]

    append_state_items(state, "recent_decisions", event, event.get("decisions", []), limit=24)
    append_state_items(state, "open_questions", event, event.get("questions", []), limit=24)
    append_state_items(state, "pending_promises", event, event.get("promises", []), limit=24)


def append_state_items(
    state: dict[str, Any], field: str, event: dict[str, Any], texts: list[str], *, limit: int
) -> None:
    existing = list(state.get(field, []))
    for text in texts:
        existing.append(
            {
                "event_id": event["id"],
                "timestamp_ms": event["timestamp_ms"],
                "topic_id": event["topic_id"],
                "text": text,
            }
        )
    state[field] = existing[-limit:]


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
