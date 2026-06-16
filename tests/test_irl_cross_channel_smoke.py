import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/tikawamoto/.hermes/hermes-agent/venv/bin/python3"


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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def write_cross_channel_fixture(store: Path) -> None:
    topic_id = "oac-irl-smoke"
    write_jsonl(
        store / "events.jsonl",
        [
            {
                "id": "tg-start",
                "timestamp_ms": 1000,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Ti started the OAC IRL cross-channel smoke test from Telegram.",
                "sensitivity": "private",
                "topic_id": topic_id,
                "continuity_intent": "continue_topic",
                "facts": [{"key": "scope.gateway_hooks", "value": "include"}],
            },
            {
                "id": "tg-stale-plan",
                "timestamp_ms": 1100,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Hermes",
                "canonical_user_id": "ti",
                "role": "assistant",
                "summary": "Temporary smoke plan included a gateway hook check.",
                "sensitivity": "private",
                "topic_id": topic_id,
                "continuity_intent": "continue_topic",
                "decay_after_ms": 500,
                "decay_reason": "Superseded by local-first smoke scope.",
            },
            {
                "id": "sig-private-detail",
                "timestamp_ms": 1600,
                "surface": "signal",
                "channel_id": "dm-ti",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Signal DM has private smoke detail with pretend secret ORCHID-123.",
                "sensitivity": "secret",
                "topic_id": topic_id,
                "continuity_intent": "continue_topic",
                "facts": [{"key": "smoke.private_marker", "value": "ORCHID-123"}],
            },
            {
                "id": "local-scope-correction",
                "timestamp_ms": 2200,
                "surface": "local",
                "channel_id": "cli",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Local CLI fixture says continue OAC cross-channel smoke with no gateway hooks yet.",
                "sensitivity": "private",
                "topic_id": topic_id,
                "continuity_intent": "continue_topic",
                "facts": [{"key": "scope.gateway_hooks", "value": "exclude"}],
            },
            {
                "id": "other-user-same-topic",
                "timestamp_ms": 2300,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Olivia Woody",
                "canonical_user_id": "olivia",
                "role": "user",
                "summary": "LEAK_OTHER_USER should never appear in Ti's smoke artifact.",
                "sensitivity": "private",
                "topic_id": topic_id,
                "continuity_intent": "continue_topic",
            },
            {
                "id": "tg-return",
                "timestamp_ms": 3000,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Ti returned to Telegram and said alright let's go for it.",
                "sensitivity": "private",
                "topic_id": topic_id,
                "continuity_intent": "continue_topic",
            },
        ],
    )
    (store / "state.json").write_text(
        json.dumps(
            {
                "version": 2,
                "current_focus": ["OAC MVP for Telegram/Signal/SMS/voice continuity"],
                "surface_policies": {
                    "telegram": {"trust": "low", "room_scope": "group"},
                    "signal": {"trust": "high", "room_scope": "dm"},
                    "local": {"trust": "high", "room_scope": "local"},
                },
                "topics": {
                    "ti\u001foac-irl-smoke": {
                        "canonical_user_id": "ti",
                        "topic_id": topic_id,
                        "title": "OAC IRL cross-channel smoke test",
                        "summary": "Verify Telegram, Signal, and local continuity agree without leaking high-trust context.",
                        "sensitivity": "public",
                        "last_event_id": "tg-return",
                        "last_updated_ms": 3000,
                    },
                    "olivia\u001foac-irl-smoke": {
                        "canonical_user_id": "olivia",
                        "topic_id": topic_id,
                        "title": "Other user topic",
                        "summary": "LEAK_OTHER_TOPIC_METADATA",
                        "last_event_id": "other-user-same-topic",
                        "last_updated_ms": 2300,
                    },
                },
                "active_topic_ids": [topic_id],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_smoke_command_writes_cross_channel_artifact_and_verifies_privacy(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    out_path = tmp_path / "smoke-report.json"
    write_cross_channel_fixture(store)

    result = run_oac(
        "smoke",
        "--store",
        str(store),
        "--out",
        str(out_path),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "alright let's go for it",
        "--as-of-ms",
        "4000",
        "--forbidden-string",
        "ORCHID-123",
        "--forbidden-string",
        "LEAK_OTHER_USER",
        "--forbidden-string",
        "LEAK_OTHER_TOPIC_METADATA",
        "--forbidden-string",
        "Local CLI fixture says continue OAC cross-channel smoke with no gateway hooks yet.",
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "smoke-report.json" in result.stdout

    report = json.loads(out_path.read_text(encoding="utf-8"))
    rendered_report = json.dumps(report, sort_keys=True)
    assert "ORCHID-123" not in rendered_report
    assert "LEAK_OTHER_USER" not in rendered_report
    assert "LEAK_OTHER_TOPIC_METADATA" not in rendered_report

    assert report["artifact_type"] == "continuity_smoke_report"
    assert report["generated_at_ms"] == 4000
    assert report["surface_policy"] == {"surface": "telegram", "trust": "low", "room_scope": "group"}
    assert report["canonical_user_id"] == "ti"
    assert report["likely_continuation"] == "OAC IRL cross-channel smoke test"
    assert report["cross_surface_sources"] == ["local", "signal", "telegram"]
    assert report["checks"] == {
        "context_matches_digest_markdown": True,
        "cross_surface_sources_present": True,
        "sensitive_context_present": True,
        "contradictions_present": True,
        "decay_present": True,
        "unrelated_user_excluded": True,
        "redaction_ok": True,
    }
    assert report["digest"]["source_event_ids"] == [
        "tg-start",
        "tg-stale-plan",
        "sig-private-detail",
        "local-scope-correction",
        "tg-return",
    ]
    assert report["digest"]["recent_safe_events"][-1]["id"] == "tg-return"
    assert report["digest"]["sensitive_context"] == [
        {
            "event_id": "sig-private-detail",
            "surface": "signal",
            "message": "Sensitive context exists on a higher-trust surface; use Signal/local before acting.",
        },
        {
            "event_id": "local-scope-correction",
            "surface": "local",
            "message": "Sensitive context exists on a higher-trust surface; use Signal/local before acting.",
        },
    ]
    assert report["context_markdown"] == report["digest"]["markdown"]


def test_smoke_command_fails_when_required_checks_do_not_pass(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    out_path = tmp_path / "smoke-report.json"
    write_jsonl(
        store / "events.jsonl",
        [
            {
                "id": "tg-only",
                "timestamp_ms": 1000,
                "surface": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "role": "user",
                "summary": "Telegram-only smoke fixture lacks cross-channel evidence.",
                "sensitivity": "private",
                "topic_id": "oac-irl-smoke",
                "continuity_intent": "continue_topic",
            }
        ],
    )
    (store / "state.json").write_text(json.dumps({"version": 2}), encoding="utf-8")

    result = run_oac(
        "smoke",
        "--store",
        str(store),
        "--out",
        str(out_path),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "smoke",
        "--as-of-ms",
        "4000",
    )

    assert result.returncode == 1
    assert "Smoke checks failed" in result.stderr
    assert out_path.exists()
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["checks"]["sensitive_context_present"] is False
    assert report["checks"]["cross_surface_sources_present"] is False
    assert report["checks"]["contradictions_present"] is False
    assert report["checks"]["decay_present"] is False


def test_smoke_failure_artifact_redacts_forbidden_strings_before_writing(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    out_path = tmp_path / "smoke-report.json"
    write_cross_channel_fixture(store)

    result = run_oac(
        "smoke",
        "--store",
        str(store),
        "--out",
        str(out_path),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "alright let's go for it",
        "--as-of-ms",
        "4000",
        "--forbidden-string",
        "Ti returned to Telegram and said alright let's go for it.",
    )

    assert result.returncode == 1
    report_text = out_path.read_text(encoding="utf-8")
    assert "Ti returned to Telegram and said alright let's go for it." not in report_text
    assert "[FORBIDDEN-STRING-REDACTED]" in report_text
    report = json.loads(report_text)
    assert report["checks"]["redaction_ok"] is False
