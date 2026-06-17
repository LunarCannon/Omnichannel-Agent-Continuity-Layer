from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from .identity import resolve_identity_alias
from .record import file_lock, record_event
from .synthesize import load_jsonl, load_state, migrate_state, atomic_write_json

OAC_HEADER_RE = re.compile(r"(^|\n)\s*##\s+Omnichannel Agent Continuity\b")
LEGACY_OAC_HEADER_RE = re.compile(r"(^|\n)\s*Omnichannel Agent Continuity\s*\nThe following\b")
SECRETISH_RE = re.compile(
    r"\b((?:vault\s+)?(?:code|secret|token|password|api\s+key|credential)(?:\s+(?:is|=|:))\s+)([A-Za-z0-9_:/+=-]{3,})",
    re.IGNORECASE,
)
ASSISTANT_PROCESS_CHATTER_RE = re.compile(
    r"(?ix)^\s*(?:"
    r"checking\b|"
    r"running\b|"
    r"patching\b|"
    r"verified\s*:\s*|"
    r"i(?:'|’)?m\s+(?:doing|checking|adding|patching|running|testing)\b|"
    r"i(?:'|’)?ll\s+(?:check|inspect|run|patch|tighten|verify)\b|"
    r"let\s+me\s+(?:check|inspect|run|patch|verify)\b"
    r")"
)
LAST_SYNC_FIELD = "last_synced_state_db_message_id"


def sync_state_db(
    *,
    store: Path,
    state_db: Path,
    limit: int = 500,
    full: bool = False,
) -> dict[str, Any]:
    if not state_db.exists():
        return base_report(status="missing_state_db", state_db=state_db, limit=limit)

    existing_ids = event_ids(store / "events.jsonl")
    store.mkdir(parents=True, exist_ok=True)
    with file_lock(store / ".lock"):
        state = migrate_state(load_state(store / "state.json"))
        stored_last = state.get(LAST_SYNC_FIELD)
        stored_last_id = int(stored_last or 0)
        bootstrap_last_id = max_existing_state_db_id(existing_ids) if stored_last is None else stored_last_id
        last_id = 0 if full else bootstrap_last_id

    rows = fetch_rows(state_db=state_db, after_id=last_id, limit=limit)
    report = base_report(status="ok", state_db=state_db, limit=limit)
    report["last_message_id"] = last_id

    unresolved_identity_ids: list[int] = []
    max_seen = last_id
    for row in rows:
        message_id = int(row["id"])
        max_seen = max(max_seen, message_id)
        role = str(row["role"] or "")
        surface = str(row["source"] or "unknown")
        if surface == "cron":
            report["skipped_cron"] += 1
            continue
        if role not in {"user", "assistant"}:
            report["skipped_role"] += 1
            continue
        if int(row["active"] or 0) != 1:
            report["skipped_inactive"] += 1
            continue

        event_id = f"hermes:state-db:{message_id}"
        if event_id in existing_ids:
            report["already_seen"] += 1
            continue

        channel_id = str(row["user_id"] or "")
        sender = channel_id
        canonical_user_id = resolve_sync_identity(store=store, surface=surface, channel_id=channel_id, sender=sender)
        if not canonical_user_id:
            report["skipped_no_identity"] += 1
            unresolved_identity_ids.append(message_id)
            continue

        summary = message_summary(str(row["content"] or ""), role)
        if not summary:
            report["skipped_empty_summary"] += 1
            continue

        recorded = record_event(
            store=store,
            surface=surface,
            channel_id=channel_id,
            sender=sender,
            canonical_user_id=canonical_user_id,
            role=role,
            summary=summary,
            topic_id=topic_id_from_row(row),
            topic_title=topic_title_from_row(row),
            sensitivity="private",
            timestamp_ms=int(float(row["timestamp"] or time.time()) * 1000),
            event_id=event_id,
            continuity_intent="note",
            modality="text",
        )
        existing_ids.add(event_id)
        if recorded.get("_oac_duplicate") is True:
            report["already_seen"] += 1
        else:
            report["synced"] += 1

    cursor_target = min(unresolved_identity_ids) - 1 if unresolved_identity_ids else max_seen
    with file_lock(store / ".lock"):
        state = migrate_state(load_state(store / "state.json"))
        if state.get(LAST_SYNC_FIELD) != cursor_target:
            state[LAST_SYNC_FIELD] = cursor_target
            atomic_write_json(store / "state.json", state)

    report["max_message_id"] = max_seen
    report["cursor_message_id"] = cursor_target
    return report


def fetch_rows(*, state_db: Path, after_id: int, limit: int) -> list[sqlite3.Row]:
    uri = f"{state_db.resolve().as_uri()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            """
            SELECT m.id, m.session_id, m.role, m.content, m.timestamp, m.platform_message_id, m.active,
                   s.source, s.title, s.user_id
            FROM messages m
            LEFT JOIN sessions s ON s.id = m.session_id
            WHERE m.id > ?
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (after_id, max(1, int(limit))),
        ).fetchall()
    finally:
        con.close()


def event_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    if not path.exists():
        return ids
    for event in load_jsonl(path):
        event_id = str(event.get("id") or "")
        if event_id:
            ids.add(event_id)
    return ids


def max_existing_state_db_id(ids: set[str]) -> int:
    max_id = 0
    for event_id in ids:
        if not event_id.startswith("hermes:state-db:"):
            continue
        try:
            max_id = max(max_id, int(event_id.rsplit(":", 1)[1]))
        except ValueError:
            continue
    return max_id


def resolve_sync_identity(*, store: Path, surface: str, channel_id: str, sender: str) -> str:
    if not surface or not channel_id or not sender:
        return ""
    try:
        state = load_state(store / "state.json")
        return resolve_identity_alias(state=state, surface=surface, channel_id=channel_id, sender=sender)
    except ValueError:
        return ""


def message_summary(content: str, role: str) -> str:
    stripped = strip_injected_oac_context(content).strip()
    if not stripped:
        return ""
    if stripped.startswith("[CONTEXT COMPACTION") or stripped.startswith("## Active Task"):
        return ""
    if stripped.startswith("{") and len(stripped) > 300:
        return ""
    if role == "assistant" and ASSISTANT_PROCESS_CHATTER_RE.match(stripped):
        return ""
    redacted = SECRETISH_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", stripped)
    return " ".join(redacted.split())[:240]


def strip_injected_oac_context(text: str) -> str:
    for pattern in (OAC_HEADER_RE, LEGACY_OAC_HEADER_RE):
        match = pattern.search(text)
        if match:
            return text[: match.start()].strip()
    return text.strip()


def topic_id_from_row(row: sqlite3.Row) -> str:
    title = sanitized_title(str(row["title"] or ""))
    if title and not has_control_chars(title):
        return title[:120]
    session_id = str(row["session_id"] or "").strip()
    if session_id and not has_control_chars(session_id):
        return session_id[:120]
    return "state-db-sync"


def topic_title_from_row(row: sqlite3.Row) -> str | None:
    title = sanitized_title(str(row["title"] or ""))
    return title or None


def sanitized_title(title: str) -> str:
    stripped = strip_injected_oac_context(title)
    redacted = SECRETISH_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", stripped)
    return " ".join(redacted.split())[:120]


def has_control_chars(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def base_report(*, status: str, state_db: Path, limit: int) -> dict[str, Any]:
    return {
        "artifact_type": "state_db_sync_report",
        "version": 1,
        "status": status,
        "state_db": str(state_db),
        "limit": limit,
        "last_message_id": 0,
        "max_message_id": 0,
        "cursor_message_id": 0,
        "synced": 0,
        "already_seen": 0,
        "skipped_cron": 0,
        "skipped_role": 0,
        "skipped_inactive": 0,
        "skipped_no_identity": 0,
        "skipped_empty_summary": 0,
        "delivery_action": "none",
    }
