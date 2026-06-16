from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .context import build_context_brief
from .synthesize import atomic_write_json, load_jsonl, load_state, synthesize_digest


def run_smoke_check(
    *,
    store: Path,
    out: Path,
    surface: str,
    canonical_user_id: str,
    query: str = "",
    as_of_ms: int | None = None,
    max_events: int = 12,
    forbidden_strings: list[str] | None = None,
) -> tuple[bool, dict[str, Any]]:
    events_path = store / "events.jsonl"
    state_path = store / "state.json"
    events = load_jsonl(events_path)
    state = load_state(state_path)
    digest = synthesize_digest(
        events=events,
        state=state,
        surface=surface,
        canonical_user_id=canonical_user_id,
        query=query,
        as_of_ms=as_of_ms,
        max_events=max_events,
    )
    context_markdown = build_context_brief(
        store=store,
        surface=surface,
        canonical_user_id=canonical_user_id,
        query=query,
        as_of_ms=as_of_ms,
        max_chars=200_000,
        max_events=max_events,
    )

    source_event_ids = [str(event_id) for event_id in digest.get("source_event_ids", [])]
    source_events = [event for event in events if str(event.get("id", "")) in source_event_ids]
    cross_surface_sources = sorted({str(event.get("surface", "")) for event in source_events if event.get("surface")})
    unrelated_user_excluded = all(
        str(event.get("canonical_user_id", "")) == canonical_user_id for event in source_events
    )

    checks = {
        "context_matches_digest_markdown": context_markdown == digest.get("markdown", ""),
        "cross_surface_sources_present": len(cross_surface_sources) >= 2,
        "sensitive_context_present": bool(digest.get("sensitive_context")),
        "contradictions_present": bool(digest.get("contradictions")),
        "decay_present": bool(digest.get("decay")),
        "unrelated_user_excluded": unrelated_user_excluded,
        "redaction_ok": True,
    }

    report: dict[str, Any] = {
        "artifact_type": "continuity_smoke_report",
        "version": 1,
        "generated_at_ms": digest.get("generated_at_ms"),
        "surface_policy": digest.get("surface_policy"),
        "canonical_user_id": canonical_user_id,
        "query": query,
        "likely_continuation": digest.get("likely_continuation"),
        "topic_id": digest.get("topic_id"),
        "cross_surface_sources": cross_surface_sources,
        "checks": checks,
        "digest": digest,
        "context_markdown": context_markdown,
    }

    checks["redaction_ok"] = forbidden_strings_absent(report, forbidden_strings or [])
    if not checks["redaction_ok"]:
        report = redact_forbidden_strings(report, forbidden_strings or [])
        checks = report["checks"]
    passed = all(checks.values())
    atomic_write_json(out, report)
    return passed, report


def forbidden_strings_absent(report: dict[str, Any], forbidden_strings: list[str]) -> bool:
    rendered = json.dumps(report, sort_keys=True)
    return all(value not in rendered for value in forbidden_strings if value)


def redact_forbidden_strings(value: Any, forbidden_strings: list[str]) -> Any:
    if isinstance(value, str):
        redacted = value
        for forbidden in forbidden_strings:
            if forbidden:
                redacted = redacted.replace(forbidden, "[FORBIDDEN-STRING-REDACTED]")
        return redacted
    if isinstance(value, list):
        return [redact_forbidden_strings(item, forbidden_strings) for item in value]
    if isinstance(value, dict):
        return {key: redact_forbidden_strings(item, forbidden_strings) for key, item in value.items()}
    return value
