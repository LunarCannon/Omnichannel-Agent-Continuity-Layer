import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_oac(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "oac.cli", *args],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_appends_event_and_updates_rolling_state(tmp_path: Path) -> None:
    store = tmp_path / ".oac"

    result = run_oac(
        "record",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Ti Kawamoto",
        "--canonical-user-id",
        "ti",
        "--role",
        "user",
        "--summary",
        "Ti picked the local record slice.",
        "--topic-id",
        "oac-record",
        "--topic-title",
        "OAC local record slice",
        "--sensitivity",
        "private",
        "--timestamp-ms",
        "4000",
        "--decision",
        "Build record before context or gateway hooks.",
        "--question",
        "What should context emit next?",
    )

    assert result.returncode == 0, result.stderr
    assert "Recorded event" in result.stdout

    events = read_jsonl(store / "events.jsonl")
    assert len(events) == 1
    event = events[0]
    assert event == {
        "id": event["id"],
        "timestamp_ms": 4000,
        "surface": "telegram",
        "channel_id": "thread-1340",
        "sender": "Ti Kawamoto",
        "canonical_user_id": "ti",
        "role": "user",
        "summary": "Ti picked the local record slice.",
        "sensitivity": "private",
        "topic_id": "oac-record",
        "continuity_intent": "continue_topic",
        "modality": "text",
        "decisions": ["Build record before context or gateway hooks."],
        "questions": ["What should context emit next?"],
    }
    assert event["id"] == "evt_68a1c93fd9773154"

    state = json.loads((store / "state.json").read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["active_topic_ids"] == ["oac-record"]
    assert state["topics"]["ti\u001foac-record"] == {
        "canonical_user_id": "ti",
        "topic_id": "oac-record",
        "title": "OAC local record slice",
        "summary": "Ti picked the local record slice.",
        "last_event_id": event["id"],
        "last_updated_ms": 4000,
        "sensitivity": "private",
        "surface": "telegram",
    }
    assert state["recent_decisions"] == [
        {
            "event_id": event["id"],
            "timestamp_ms": 4000,
            "topic_id": "oac-record",
            "text": "Build record before context or gateway hooks.",
        }
    ]
    assert state["open_questions"] == [
        {
            "event_id": event["id"],
            "timestamp_ms": 4000,
            "topic_id": "oac-record",
            "text": "What should context emit next?",
        }
    ]


def test_record_redacts_credential_like_text_before_writing_ledger(tmp_path: Path) -> None:
    store = tmp_path / ".oac"

    result = run_oac(
        "record",
        "--store",
        str(store),
        "--surface",
        "signal",
        "--channel-id",
        "dm-ti",
        "--sender",
        "Ti Kawamoto",
        "--canonical-user-id",
        "ti",
        "--role",
        "user",
        "--summary",
        "Fake sensitive note: pretend vault code is BANANA-123.",
        "--topic-id",
        "oac-record",
        "--sensitivity",
        "secret",
        "--timestamp-ms",
        "4100",
        "--fact",
        "fake_secret=BANANA-123",
    )

    assert result.returncode == 0, result.stderr
    ledger_text = (store / "events.jsonl").read_text(encoding="utf-8")
    state_text = (store / "state.json").read_text(encoding="utf-8")
    assert "BANANA-123" not in ledger_text
    assert "BANANA-123" not in state_text

    event = read_jsonl(store / "events.jsonl")[0]
    assert event["summary"] == "Fake sensitive note: pretend vault code is [REDACTED]."
    assert event["facts"] == [{"key": "fake_secret", "value": "[REDACTED]"}]


def test_record_voice_event_keeps_modality_and_artifact_without_provider_details(tmp_path: Path) -> None:
    store = tmp_path / ".oac"

    result = run_oac(
        "record",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-35",
        "--sender",
        "Destructor",
        "--canonical-user-id",
        "ti",
        "--role",
        "assistant",
        "--summary",
        "Destructor sent a voice reply about the OAC integration slice.",
        "--topic-id",
        "destructor-voice",
        "--topic-title",
        "Destructor voice",
        "--sensitivity",
        "private",
        "--timestamp-ms",
        "4200",
        "--modality",
        "voice",
        "--artifact-ref",
        "local:///tmp/destructor-voice.ogg",
    )

    assert result.returncode == 0, result.stderr
    ledger_text = (store / "events.jsonl").read_text(encoding="utf-8")
    assert "supertonic" not in ledger_text.lower()
    assert "opus" not in ledger_text.lower()
    event = read_jsonl(store / "events.jsonl")[0]
    assert event["modality"] == "voice"
    assert event["artifact_ref"] == "local:///tmp/destructor-voice.ogg"
    assert event["summary"] == "Destructor sent a voice reply about the OAC integration slice."


def test_recorded_event_can_be_synthesized_from_store_paths(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    record_result = run_oac(
        "record",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Ti Kawamoto",
        "--canonical-user-id",
        "ti",
        "--role",
        "user",
        "--summary",
        "Ti picked the local record slice.",
        "--topic-id",
        "oac-record",
        "--topic-title",
        "OAC local record slice",
        "--sensitivity",
        "private",
        "--timestamp-ms",
        "4000",
    )
    assert record_result.returncode == 0, record_result.stderr

    out_path = tmp_path / "digest.json"
    synth_result = run_oac(
        "synthesize",
        "--events",
        str(store / "events.jsonl"),
        "--state",
        str(store / "state.json"),
        "--out",
        str(out_path),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "what did Ti pick?",
        "--as-of-ms",
        "5000",
    )

    assert synth_result.returncode == 0, synth_result.stderr
    digest = json.loads(out_path.read_text(encoding="utf-8"))
    assert digest["likely_continuation"] == "OAC local record slice"
    assert digest["source_event_ids"] == ["evt_68a1c93fd9773154"]
    assert digest["recent_safe_events"][0]["summary"] == "Ti picked the local record slice."
