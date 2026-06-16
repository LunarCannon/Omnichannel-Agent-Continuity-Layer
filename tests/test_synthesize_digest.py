import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def run_synthesize(tmp_path: Path, events: list[dict], *, surface: str = "telegram") -> tuple[subprocess.CompletedProcess[str], dict]:
    events_path = tmp_path / "events.jsonl"
    state_path = tmp_path / "state.json"
    out_path = tmp_path / "digest.json"
    write_jsonl(events_path, events)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "current_focus": ["OAC MVP for Telegram/Signal/SMS/voice continuity"],
                "surface_policies": {
                    "telegram": {"trust": "low", "room_scope": "group"},
                    "signal": {"trust": "high", "room_scope": "dm"},
                    "local": {"trust": "high", "room_scope": "local"},
                },
                "topics": {
                    "oac-digest": {
                        "canonical_user_id": "ti",
                        "title": "OAC continuity digest vertical slice",
                        "summary": "Build a local-first deterministic synthesize command for OAC digest artifacts.",
                    }
                },
                "active_topic_ids": ["oac-digest"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "oac.cli",
            "synthesize",
            "--events",
            str(events_path),
            "--state",
            str(state_path),
            "--out",
            str(out_path),
            "--surface",
            surface,
            "--canonical-user-id",
            "ti",
            "--query",
            "implement the OAC continuity digest vertical slice",
            "--as-of-ms",
            "3000",
        ],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    digest = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    return result, digest


def test_synthesize_writes_deterministic_digest_artifact_with_safe_recent_events(tmp_path: Path) -> None:
    events = [
        {
            "id": "sig-later",
            "timestamp_ms": 2200,
            "surface": "signal",
            "channel_id": "dm-ti",
            "canonical_user_id": "ti",
            "role": "user",
            "summary": "Ti narrowed scope to local synthesize, deterministic collection, artifacts, contradictions, decay, and tests.",
            "sensitivity": "private",
            "topic_id": "oac-digest",
            "continuity_intent": "continue_topic",
        },
        {
            "id": "tg-earlier",
            "timestamp_ms": 1000,
            "surface": "telegram",
            "channel_id": "thread-1340",
            "canonical_user_id": "ti",
            "role": "user",
            "summary": "Ti asked for the next OAC vertical slice.",
            "sensitivity": "public",
            "topic_id": "oac-digest",
            "continuity_intent": "continue_topic",
        },
    ]

    result, digest = run_synthesize(tmp_path, events)

    assert result.returncode == 0, result.stderr
    assert "digest.json" in result.stdout
    assert digest["artifact_type"] == "continuity_digest"
    assert digest["generated_at_ms"] == 3000
    assert digest["surface_policy"] == {"surface": "telegram", "trust": "low", "room_scope": "group"}
    assert digest["canonical_user_id"] == "ti"
    assert digest["likely_continuation"] == "OAC continuity digest vertical slice"
    assert digest["topic_summary"] == "Build a local-first deterministic synthesize command for OAC digest artifacts."
    assert digest["source_event_ids"] == ["tg-earlier", "sig-later"]
    assert [event["id"] for event in digest["recent_safe_events"]] == ["tg-earlier", "sig-later"]
    assert "Contradictions" in digest["markdown"]
    assert "Decay" in digest["markdown"]

    second_result, second_digest = run_synthesize(tmp_path, list(reversed(events)))
    assert second_result.returncode == 0, second_result.stderr
    assert second_digest == digest


def test_low_trust_digest_redacts_sensitive_events_but_records_presence(tmp_path: Path) -> None:
    events = [
        {
            "id": "secret-signal",
            "timestamp_ms": 1800,
            "surface": "signal",
            "channel_id": "dm-ti",
            "canonical_user_id": "ti",
            "role": "user",
            "summary": "Fake sensitive note: pretend vault code is BANANA-123.",
            "sensitivity": "secret",
            "topic_id": "oac-digest",
            "continuity_intent": "continue_topic",
            "facts": [{"key": "fake_secret", "value": "BANANA-123", "text": "Pretend vault code."}],
        },
        {
            "id": "secret-correction",
            "timestamp_ms": 1900,
            "surface": "signal",
            "channel_id": "dm-ti",
            "canonical_user_id": "ti",
            "role": "user",
            "summary": "The fake sensitive note was rotated.",
            "sensitivity": "secret",
            "topic_id": "oac-digest",
            "facts": [{"key": "fake_secret", "value": "PLANTAIN-456", "text": "Rotated pretend vault code."}],
        },
    ]

    result, digest = run_synthesize(tmp_path, events, surface="telegram")

    assert result.returncode == 0, result.stderr
    rendered = json.dumps(digest, sort_keys=True)
    assert "BANANA-123" not in rendered
    assert "PLANTAIN-456" not in rendered
    assert digest["recent_safe_events"] == []
    assert digest["sensitive_context"] == [
        {
            "event_id": "secret-signal",
            "surface": "signal",
            "message": "Sensitive context exists on a higher-trust surface; use Signal/local before acting.",
        },
        {
            "event_id": "secret-correction",
            "surface": "signal",
            "message": "Sensitive context exists on a higher-trust surface; use Signal/local before acting.",
        },
    ]
    assert digest["contradictions"] == [
        {
            "key": "fake_secret",
            "older_event_id": "secret-signal",
            "older_value": "[sensitive]",
            "newer_event_id": "secret-correction",
            "newer_value": "[sensitive]",
            "resolution": "Prefer the newer event unless the user says otherwise.",
        }
    ]


def test_digest_reports_contradictions_and_decayed_items_from_synthetic_events(tmp_path: Path) -> None:
    events = [
        {
            "id": "old-cron-in",
            "timestamp_ms": 1000,
            "surface": "telegram",
            "channel_id": "thread-1340",
            "canonical_user_id": "ti",
            "role": "assistant",
            "summary": "Initial thought: include cron delivery in the MVP.",
            "sensitivity": "private",
            "topic_id": "oac-digest",
            "facts": [{"key": "scope.cron_delivery", "value": "include", "text": "Include cron delivery in MVP."}],
        },
        {
            "id": "new-cron-out",
            "timestamp_ms": 2500,
            "surface": "telegram",
            "channel_id": "thread-1340",
            "canonical_user_id": "ti",
            "role": "user",
            "summary": "Scope correction: no cron/delivery yet.",
            "sensitivity": "private",
            "topic_id": "oac-digest",
            "facts": [{"key": "scope.cron_delivery", "value": "exclude", "text": "No cron/delivery yet."}],
        },
        {
            "id": "stale-plan",
            "timestamp_ms": 1000,
            "surface": "telegram",
            "channel_id": "thread-1340",
            "canonical_user_id": "ti",
            "role": "assistant",
            "summary": "Temporary plan: maybe implement delivery after the digest.",
            "sensitivity": "private",
            "topic_id": "oac-digest",
            "decay_after_ms": 1000,
            "decay_reason": "Superseded by vertical-slice scope.",
        },
    ]

    result, digest = run_synthesize(tmp_path, events)

    assert result.returncode == 0, result.stderr
    assert digest["contradictions"] == [
        {
            "key": "scope.cron_delivery",
            "older_event_id": "old-cron-in",
            "older_value": "include",
            "newer_event_id": "new-cron-out",
            "newer_value": "exclude",
            "resolution": "Prefer the newer event unless the user says otherwise.",
        }
    ]
    assert digest["decay"] == [
        {
            "event_id": "stale-plan",
            "age_ms": 2000,
            "reason": "Superseded by vertical-slice scope.",
            "summary": "Temporary plan: maybe implement delivery after the digest.",
        }
    ]
    assert "scope.cron_delivery" in digest["markdown"]
    assert "stale-plan" in digest["markdown"]
