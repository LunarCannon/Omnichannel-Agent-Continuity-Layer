import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/tikawamoto/.hermes/hermes-agent/venv/bin/python3"
sys.path.insert(0, str(REPO_ROOT / "src"))

from oac.synthesize import load_jsonl, load_state  # noqa: E402


def run_oac(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "oac.cli", *args],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def test_load_state_migrates_owned_legacy_topics_and_quarantines_unowned_metadata(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "topics": {
                    "owned-topic": {
                        "canonical_user_id": "ti",
                        "title": "Owned legacy topic title",
                        "summary": "Owned legacy topic summary",
                        "last_event_id": "owned-event",
                        "last_updated_ms": 6000,
                    },
                    "unowned-topic": {
                        "title": "LEGACY OTHER USER TITLE",
                        "summary": "Legacy summary leak",
                        "last_event_id": "unowned-event",
                    },
                },
                "active_topic_ids": ["owned-topic", "unowned-topic"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state = load_state(state_path)

    assert state["version"] == 2
    assert state["topics"] == {
        "ti\u001fowned-topic": {
            "canonical_user_id": "ti",
            "topic_id": "owned-topic",
            "title": "Owned legacy topic title",
            "summary": "Owned legacy topic summary",
            "last_event_id": "owned-event",
            "last_updated_ms": 6000,
            "sensitivity": "sensitive",
            "surface": "",
        }
    }
    rendered_state = json.dumps(state, sort_keys=True)
    assert "LEGACY OTHER USER TITLE" not in rendered_state
    assert "Legacy summary leak" not in rendered_state
    assert state["quarantined_legacy_topics"] == [
        {"reason": "missing canonical_user_id", "topic_id": "unowned-topic"}
    ]


def test_load_state_quarantines_null_owner_without_preserving_metadata(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "topics": {
                    "null-owner-topic": {
                        "canonical_user_id": None,
                        "title": "NULL OWNER SECRET TITLE",
                        "summary": "Null owner summary leak",
                    }
                },
                "active_topic_ids": ["null-owner-topic"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state = load_state(state_path)

    assert state["topics"] == {}
    assert state["quarantined_legacy_topics"] == [
        {"reason": "missing canonical_user_id", "topic_id": "null-owner-topic"}
    ]
    rendered_state = json.dumps(state, sort_keys=True)
    assert "NULL OWNER SECRET TITLE" not in rendered_state
    assert "Null owner summary leak" not in rendered_state


def test_load_state_quarantines_malformed_topic_timestamps_instead_of_crashing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "topics": {
                    "bad-time-topic": {
                        "canonical_user_id": "ti",
                        "title": "Bad timestamp title",
                        "summary": "Bad timestamp summary",
                        "last_updated_ms": "not-an-int",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state = load_state(state_path)

    assert state["topics"] == {}
    assert state["quarantined_legacy_topics"] == [
        {"reason": "invalid topic metadata", "topic_id": "bad-time-topic"}
    ]


def test_migrate_state_is_idempotent_and_preserves_quarantine_audit(tmp_path: Path) -> None:
    first = load_state(None)
    first["quarantined_legacy_topics"] = [
        {"reason": "missing canonical_user_id", "topic_id": "old-topic"}
    ]

    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps(first, sort_keys=True), encoding="utf-8")
    second_path_state = load_state(state_path)

    assert second_path_state["topics"] == {}
    assert second_path_state["quarantined_legacy_topics"] == [
        {"reason": "missing canonical_user_id", "topic_id": "old-topic"}
    ]


def test_context_uses_migrated_owned_legacy_topic_metadata(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    store.mkdir()
    write_jsonl(
        store / "events.jsonl",
        [
            {
                "id": "ti-owned-event",
                "timestamp_ms": 6000,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Ti is testing migration-on-load.",
                "sensitivity": "private",
                "topic_id": "owned-topic",
            }
        ],
    )
    (store / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "identity_aliases": {"telegram:thread-1340:Ti Kawamoto": "ti"},
                "topics": {
                    "owned-topic": {
                        "canonical_user_id": "ti",
                        "title": "Migrated owned topic title",
                        "summary": "Migrated owned topic summary",
                    }
                },
                "active_topic_ids": ["owned-topic"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "local",
        "--canonical-user-id",
        "ti",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Ti Kawamoto",
        "--query",
        "migration load",
        "--as-of-ms",
        "7000",
    )

    assert result.returncode == 0, result.stderr
    assert "Likely continuation: Migrated owned topic title" in result.stdout
    assert "Topic summary: Migrated owned topic summary" in result.stdout
    assert "Ti is testing migration-on-load." in result.stdout


def test_load_jsonl_migrates_legacy_event_defaults_and_drops_unowned_or_poisoned_events(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    write_jsonl(
        events_path,
        [
            {
                "id": "legacy-valid",
                "timestamp_ms": "6000",
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Legacy valid event should survive migration.",
                "topic_id": "owned-topic",
            },
            {
                "id": "missing-user",
                "timestamp_ms": 6100,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Unknown",
                "role": "user",
                "summary": "Missing canonical user should be quarantined by omission.",
                "topic_id": "owned-topic",
            },
            {
                "id": "poisoned-topic",
                "timestamp_ms": 6200,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Poisoned topic key should be quarantined by omission.",
                "topic_id": "owned\u001fother",
            },
            {
                "id": "bad-sensitivity",
                "timestamp_ms": 6300,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Bad sensitivity should not fail open as private.",
                "sensitivity": "secret ",
                "topic_id": "owned-topic",
            },
            {
                "id": "bad-timestamp",
                "timestamp_ms": "not-an-int",
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Bad timestamp should be quarantined by omission.",
                "topic_id": "owned-topic",
            },
        ],
    )

    events = load_jsonl(events_path)

    assert events == [
        {
            "id": "legacy-valid",
            "timestamp_ms": 6000,
            "surface": "telegram",
            "channel_id": "thread-1340",
            "sender": "Ti Kawamoto",
            "canonical_user_id": "ti",
            "role": "user",
            "summary": "Legacy valid event should survive migration.",
            "sensitivity": "private",
            "topic_id": "owned-topic",
            "continuity_intent": "continue_topic",
            "modality": "text",
            "schema_version": 2,
        }
    ]


def test_quarantine_audit_sanitizes_new_control_character_topic_ids(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "topics": {
                    "bad\u001ftopic": {
                        "canonical_user_id": "ti",
                        "title": "Bad audit title",
                        "summary": "Bad audit summary",
                    },
                    "bad\u001fmetadata": "not-a-dict",
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state = load_state(state_path)

    assert state["topics"] == {}
    assert state["quarantined_legacy_topics"] == [
        {"reason": "invalid topic metadata", "topic_id": "[invalid]"},
        {"reason": "invalid topic identity", "topic_id": "[invalid]"},
    ]
    rendered_state = json.dumps(state, sort_keys=True)
    assert "\u001f" not in rendered_state
    assert "Bad audit title" not in rendered_state
    assert "Bad audit summary" not in rendered_state


def test_load_state_quarantines_present_but_invalid_falsy_topic_timestamp(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "topics": {
                    "empty-time-topic": {
                        "canonical_user_id": "ti",
                        "title": "Empty timestamp title",
                        "summary": "Empty timestamp summary",
                        "last_updated_ms": "",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    state = load_state(state_path)

    assert state["topics"] == {}
    assert state["quarantined_legacy_topics"] == [
        {"reason": "invalid topic metadata", "topic_id": "empty-time-topic"}
    ]


def test_malformed_surface_policy_fails_closed_for_low_trust_surface(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    store.mkdir()
    write_jsonl(
        store / "events.jsonl",
        [
            {
                "id": "signal-secret",
                "timestamp_ms": 6000,
                "surface": "signal",
                "channel_id": "dm-ti",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Sensitive con: launch code is swordfish",
                "sensitivity": "secret",
                "topic_id": "oac-hardening",
            }
        ],
    )
    (store / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "surface_policies": {"telegram": {}},
                "topics": {
                    "oac-hardening": {
                        "canonical_user_id": "ti",
                        "title": "OAC hardening",
                        "summary": "Fail closed surface policy test",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "oac hardening",
        "--as-of-ms",
        "7000",
    )

    assert result.returncode == 0, result.stderr
    assert "Surface policy: telegram / low / group" in result.stdout
    assert "Sensitive context exists on a higher-trust surface" in result.stdout
    assert "launch code is swordfish" not in result.stdout


def test_low_trust_context_does_not_render_unclassified_legacy_topic_metadata(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    store.mkdir()
    write_jsonl(
        store / "events.jsonl",
        [
            {
                "id": "safe-telegram",
                "timestamp_ms": 6000,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Ti is testing legacy topic metadata fail-closed behavior.",
                "sensitivity": "private",
                "topic_id": "legacy-secret-topic",
            }
        ],
    )
    (store / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "topics": {
                    "legacy-secret-topic": {
                        "canonical_user_id": "ti",
                        "title": "LEGACY SECRET TITLE ORCHID-123",
                        "summary": "LEGACY SECRET SUMMARY ORCHID-123",
                    }
                },
                "active_topic_ids": ["legacy-secret-topic"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "legacy topic",
        "--as-of-ms",
        "7000",
    )

    assert result.returncode == 0, result.stderr
    assert "Likely continuation: legacy-secret-topic" in result.stdout
    assert "LEGACY SECRET TITLE" not in result.stdout
    assert "LEGACY SECRET SUMMARY" not in result.stdout
    assert "ORCHID-123" not in result.stdout


def test_low_trust_context_does_not_render_legacy_unscoped_current_focus(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    store.mkdir()
    write_jsonl(
        store / "events.jsonl",
        [
            {
                "id": "safe-telegram",
                "timestamp_ms": 6000,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Ti is testing current focus fail-closed behavior.",
                "sensitivity": "private",
                "topic_id": "focus-test",
            }
        ],
    )
    (store / "state.json").write_text(
        json.dumps(
            {
                "version": 1,
                "current_focus": ["OTHER_USER_SECRET_FOCUS ORCHID-123"],
                "topics": {
                    "focus-test": {
                        "canonical_user_id": "ti",
                        "title": "Focus test",
                        "summary": "Focus test summary",
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "focus test",
        "--as-of-ms",
        "7000",
    )

    assert result.returncode == 0, result.stderr
    assert "OTHER_USER_SECRET_FOCUS" not in result.stdout
    assert "ORCHID-123" not in result.stdout
    assert "### Current focus\n- None" in result.stdout
