import importlib.util
import json
import os
import subprocess
import asyncio
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = "/home/tikawamoto/.hermes/hermes-agent/venv/bin/python3"
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from test_irl_cross_channel_smoke import write_cross_channel_fixture  # noqa: E402
from oac.gateway_hook import run_gateway_hook_smoke, run_with_timeout  # noqa: E402


def run_oac(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = {"PYTHONPATH": str(REPO_ROOT / "src"), **(env or {})}
    return subprocess.run(
        [PYTHON, "-m", "oac.cli", *args],
        cwd=REPO_ROOT,
        env=merged_env,
        text=True,
        capture_output=True,
        check=False,
    )


def write_agent_start_event(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "event_type": "agent:start",
                "platform": "telegram",
                "channel_id": "thread-1340",
                "sender": "Ti Kawamoto",
                "user_id": "Ti Kawamoto",
                "canonical_user_id": "ti",
                "session_id": "session-123",
                "message": "awesome, ok yeah let's move forward with the gateway hook design",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_gateway_hook_context_command_writes_local_fail_open_artifact_when_enabled(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    write_agent_start_event(event_path)

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        "--timeout-ms",
        "250",
        "--max-chars",
        "4000",
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "hook-context.json" in result.stdout

    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    rendered = json.dumps(artifact, sort_keys=True)
    assert "ORCHID-123" not in rendered
    assert "LEAK_OTHER_USER" not in rendered
    assert "LEAK_OTHER_TOPIC_METADATA" not in rendered
    assert "Local CLI fixture says continue OAC cross-channel smoke with no gateway hooks yet." not in rendered

    assert artifact["artifact_type"] == "gateway_hook_context"
    assert artifact["enabled"] is True
    assert artifact["status"] == "context_ready"
    assert artifact["event_type"] == "agent:start"
    assert artifact["platform"] == "telegram"
    assert artifact["canonical_user_id"] == "ti"
    assert artifact["timeout_ms"] == 250
    assert artifact["fail_open"] is True
    assert artifact["delivery_action"] == "none"
    assert artifact["hook_contract"] == {
        "manifest": "HOOK.yaml",
        "handler": "handler.py",
        "hermes_event": "agent:start",
        "handler_function": "handle(event_type: str, context: dict)",
    }
    assert artifact["context_markdown"].startswith("## Omnichannel Agent Continuity")
    assert "Sensitive context exists on a higher-trust surface" in artifact["context_markdown"]


def test_gateway_hook_context_accepts_real_hermes_agent_start_shape_with_deterministic_alias(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start-hermes-shape.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    alias_result = run_oac(
        "alias",
        "set",
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
    )
    assert alias_result.returncode == 0, alias_result.stderr
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:start",
                "platform": "telegram",
                "chat_id": "group-ignored-when-thread-present",
                "thread_id": "thread-1340",
                "chat_type": "forum",
                "user_id": "Ti Kawamoto",
                "session_id": "session-123",
                "message": "runtime-shaped gateway event",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "context_ready"
    assert artifact["canonical_user_id"] == "ti"
    assert artifact["context_markdown"].startswith("## Omnichannel Agent Continuity")
    assert "ORCHID-123" not in json.dumps(artifact, sort_keys=True)


def test_gateway_hook_context_refuses_to_infer_identity_without_canonical_or_alias(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start-hermes-shape.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:start",
                "platform": "telegram",
                "chat_id": "group-ignored-when-thread-present",
                "thread_id": "thread-1340",
                "chat_type": "forum",
                "user_id": "Ti Kawamoto",
                "session_id": "session-123",
                "message": "runtime-shaped gateway event",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "no_identity"
    assert artifact["canonical_user_id"] == ""
    assert artifact["context_markdown"] == ""
    assert "ORCHID-123" not in json.dumps(artifact, sort_keys=True)


def read_events(store: Path) -> list[dict]:
    events_path = store / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_gateway_hook_context_missing_identity_fields_fails_open_with_no_identity_artifact(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start-missing-identity.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    event_path.write_text(
        json.dumps({"event_type": "agent:start", "platform": "telegram", "message": "missing identity fields"}),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "no_identity"
    assert artifact["context_markdown"] == ""
    assert artifact["fail_open"] is True


def test_gateway_hook_record_command_records_agent_start_into_v1_store_and_strips_injected_context(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "record-report.json"
    alias_result = run_oac(
        "alias",
        "set",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "157667527",
        "--canonical-user-id",
        "ti",
    )
    assert alias_result.returncode == 0, alias_result.stderr
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:start",
                "platform": "telegram",
                "chat_id": "group-chat",
                "thread_id": "thread-1340",
                "chat_type": "forum",
                "user_id": "157667527",
                "session_id": "session-123",
                "message": "let's keep wiring ingestion\n\n## Omnichannel Agent Continuity\nORCHID-123 should not be re-recorded",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "record",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "recorded"
    assert report["event_type"] == "agent:start"
    assert report["recorded_event_id"]
    events = read_events(store)
    assert len(events) == 1
    event = events[0]
    assert event["surface"] == "telegram"
    assert event["channel_id"] == "thread-1340"
    assert event["sender"] == "157667527"
    assert event["canonical_user_id"] == "ti"
    assert event["role"] == "user"
    assert event["topic_id"] == "session-123"
    assert event["summary"] == "let's keep wiring ingestion"
    rendered = json.dumps({"report": report, "events": events}, sort_keys=True)
    assert "ORCHID-123" not in rendered
    assert "Omnichannel Agent Continuity" not in rendered


def test_gateway_hook_record_command_records_agent_end_response_into_v1_store(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-end.json"
    out_path = tmp_path / "record-report.json"
    alias_result = run_oac(
        "alias",
        "set",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "group-chat",
        "--sender",
        "157667527",
        "--canonical-user-id",
        "ti",
    )
    assert alias_result.returncode == 0, alias_result.stderr
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:end",
                "platform": "telegram",
                "chat_id": "group-chat",
                "thread_id": "thread-1340",
                "user_id": "157667527",
                "session_id": "session-123",
                "response": "ingestion is now wired into v1 store",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "record",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "recorded"
    events = read_events(store)
    assert len(events) == 1
    assert events[0]["role"] == "assistant"
    assert events[0]["summary"] == "ingestion is now wired into v1 store"
    assert events[0]["topic_id"] == "session-123"


def test_gateway_hook_record_command_strips_injected_context_when_message_starts_with_oac_header(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "record-report.json"
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:start",
                "platform": "telegram",
                "chat_id": "group-chat",
                "user_id": "157667527",
                "canonical_user_id": "ti",
                "session_id": "session-123",
                "message": "  ## Omnichannel Agent Continuity\nORCHID-123 should not be recorded",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "record",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "empty_summary"
    assert read_events(store) == []
    rendered = json.dumps({"report": report, "events": read_events(store)}, sort_keys=True)
    assert "ORCHID-123" not in rendered
    assert "Omnichannel Agent Continuity" not in rendered


def test_gateway_hook_record_command_strips_injected_context_from_agent_end_response(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-end.json"
    out_path = tmp_path / "record-report.json"
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:end",
                "platform": "telegram",
                "chat_id": "group-chat",
                "user_id": "157667527",
                "canonical_user_id": "ti",
                "session_id": "session-123",
                "response": "safe assistant summary\n## Omnichannel Agent Continuity\nORCHID-123 should not be recorded",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "record",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "recorded"
    events = read_events(store)
    assert len(events) == 1
    assert events[0]["summary"] == "safe assistant summary"
    rendered = json.dumps({"report": report, "events": events}, sort_keys=True)
    assert "ORCHID-123" not in rendered
    assert "Omnichannel Agent Continuity" not in rendered


def test_gateway_hook_record_command_agent_end_missing_response_fails_open_without_store_write(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-end.json"
    out_path = tmp_path / "record-report.json"
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:end",
                "platform": "telegram",
                "chat_id": "group-chat",
                "user_id": "157667527",
                "canonical_user_id": "ti",
                "session_id": "session-123",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "record",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "empty_summary"
    assert read_events(store) == []
    assert "None" not in json.dumps(report, sort_keys=True)


def test_gateway_hook_record_command_fails_open_without_identity(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "record-report.json"
    event_path.write_text(
        json.dumps(
            {
                "event_type": "agent:start",
                "platform": "telegram",
                "chat_id": "group-chat",
                "user_id": "unknown-user",
                "session_id": "session-123",
                "message": "do not record without identity",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "record",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(out_path.read_text(encoding="utf-8"))
    assert report["status"] == "no_identity"
    assert read_events(store) == []


def test_gateway_hook_context_command_is_disabled_by_default_and_fails_open(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    write_agent_start_event(event_path)

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={},
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["enabled"] is False
    assert artifact["status"] == "disabled"
    assert artifact["context_markdown"] == ""
    assert artifact["fail_open"] is True
    assert artifact["delivery_action"] == "none"


def test_gateway_hook_context_command_malformed_event_fails_open_with_artifact(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "bad-event.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    event_path.write_text("{not json", encoding="utf-8")

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["enabled"] is True
    assert artifact["status"] == "error"
    assert artifact["context_markdown"] == ""
    assert artifact["fail_open"] is True
    assert artifact["delivery_action"] == "none"


def test_gateway_hook_context_command_skips_non_agent_start_events(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    event_path = tmp_path / "agent-end.json"
    out_path = tmp_path / "hook-context.json"
    write_cross_channel_fixture(store)
    event_path.write_text(
        json.dumps({"event_type": "agent:end", "platform": "telegram", "message": "done"}),
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "context",
        "--store",
        str(store),
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        env={"OAC_GATEWAY_HOOKS_ENABLED": "1"},
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(out_path.read_text(encoding="utf-8"))
    assert artifact["status"] == "skipped_event"
    assert artifact["context_markdown"] == ""


def test_gateway_hook_timeout_fails_open_without_raising() -> None:
    def too_slow() -> str:
        time.sleep(0.05)
        return "late context"

    started = time.perf_counter()
    result = run_with_timeout(too_slow, timeout_ms=1)
    elapsed = time.perf_counter() - started

    assert result.timed_out is True
    assert result.value is None
    assert result.error is None
    assert elapsed < 0.03


def test_gateway_hook_timeout_does_not_keep_process_alive() -> None:
    script = """
import json
import time
from oac.gateway_hook import run_with_timeout


def too_slow():
    time.sleep(1.2)
    return 'late context'


started = time.perf_counter()
result = run_with_timeout(too_slow, timeout_ms=1)
print(json.dumps({'timed_out': result.timed_out, 'elapsed': time.perf_counter() - started}))
"""
    started = time.perf_counter()
    result = subprocess.run(
        [PYTHON, "-c", script],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["timed_out"] is True
    assert payload["elapsed"] < 0.2
    assert elapsed < 0.3


def test_gateway_hook_runtime_errors_fail_open_without_context() -> None:
    def broken() -> str:
        raise RuntimeError("boom")

    result = run_with_timeout(broken, timeout_ms=100)

    assert result.timed_out is False
    assert result.value is None
    assert result.error == "boom"


def test_gateway_hook_bundle_stages_hermes_hook_files_without_live_install(tmp_path: Path) -> None:
    stage_dir = tmp_path / "staged-hook"
    store = tmp_path / ".oac"
    artifact_dir = tmp_path / "artifacts"

    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(store),
        "--artifact-dir",
        str(artifact_dir),
        "--python",
        PYTHON,
        "--src-path",
        str(REPO_ROOT / "src"),
        "--timeout-ms",
        "250",
        "--max-chars",
        "1200",
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "Staged gateway hook bundle" in result.stdout

    hook_yaml = stage_dir / "HOOK.yaml"
    handler_py = stage_dir / "handler.py"
    install_md = stage_dir / "INSTALL.md"
    bundle_manifest = stage_dir / "bundle-manifest.json"
    assert hook_yaml.exists()
    assert handler_py.exists()
    assert install_md.exists()
    assert bundle_manifest.exists()

    manifest = json.loads(bundle_manifest.read_text(encoding="utf-8"))
    assert manifest == {
        "artifact_dir": str(artifact_dir),
        "enabled_env_var": "OAC_GATEWAY_HOOKS_ENABLED",
        "events": ["agent:start", "agent:end"],
        "fail_open": True,
        "handler": "handler.py",
        "hook_name": "oac-context",
        "live_install": False,
        "manifest": "HOOK.yaml",
        "max_chars": 1200,
        "python": PYTHON,
        "src_path": str(REPO_ROOT / "src"),
        "store": str(store),
        "timeout_ms": 250,
    }
    assert "events:" in hook_yaml.read_text(encoding="utf-8")
    assert "- agent:start" in hook_yaml.read_text(encoding="utf-8")
    assert "- agent:end" in hook_yaml.read_text(encoding="utf-8")

    handler = handler_py.read_text(encoding="utf-8")
    assert "async def handle(event_type: str, context: dict)" in handler
    assert "OAC_GATEWAY_HOOKS_ENABLED" in handler
    assert "subprocess.run(" in handler
    assert "shell=True" not in handler
    assert "send_message" not in handler
    assert "requests" not in handler
    assert "httpx" not in handler
    assert "hermes gateway" not in handler

    instructions = install_md.read_text(encoding="utf-8")
    assert "disabled by default" in instructions
    assert "Do not copy this into ~/.hermes/hooks" in instructions
    assert "OAC_GATEWAY_HOOKS_ENABLED=1" in instructions


def test_staged_gateway_hook_handler_runs_only_when_env_enabled(tmp_path: Path, monkeypatch) -> None:
    stage_dir = tmp_path / "staged-hook"
    store = tmp_path / ".oac"
    artifact_dir = tmp_path / "artifacts"
    write_cross_channel_fixture(store)
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(store),
        "--artifact-dir",
        str(artifact_dir),
        "--python",
        PYTHON,
        "--src-path",
        str(REPO_ROOT / "src"),
    )
    assert result.returncode == 0, result.stderr

    spec = importlib.util.spec_from_file_location("staged_oac_hook", stage_dir / "handler.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    event_context = {
        "platform": "telegram",
        "channel_id": "thread-1340",
        "sender": "Ti Kawamoto",
        "user_id": "Ti Kawamoto",
        "canonical_user_id": "ti",
        "session_id": "session-123",
        "message": "awesome, ok yeah let's move forward with the gateway hook design",
    }

    monkeypatch.delenv("OAC_GATEWAY_HOOKS_ENABLED", raising=False)
    asyncio.run(module.handle("agent:start", event_context))
    assert list(artifact_dir.glob("hook-context-*.json")) == []

    monkeypatch.setenv("OAC_GATEWAY_HOOKS_ENABLED", "1")
    asyncio.run(module.handle("agent:start", event_context))
    artifacts = list(artifact_dir.glob("hook-context-*.json"))
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["status"] == "context_ready"
    assert artifact["delivery_action"] == "none"
    assert "ORCHID-123" not in json.dumps(artifact, sort_keys=True)
    assert list(artifact_dir.glob("oac-gateway-event-*.json")) == []


def test_staged_gateway_hook_handler_fails_open_when_artifact_dir_is_bad(tmp_path: Path, monkeypatch) -> None:
    stage_dir = tmp_path / "staged-hook"
    store = tmp_path / ".oac"
    bad_artifact_dir = tmp_path / "artifact-file"
    bad_artifact_dir.write_text("not a directory", encoding="utf-8")
    write_cross_channel_fixture(store)
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(store),
        "--artifact-dir",
        str(bad_artifact_dir),
        "--python",
        PYTHON,
        "--src-path",
        str(REPO_ROOT / "src"),
    )
    assert result.returncode == 0, result.stderr

    spec = importlib.util.spec_from_file_location("staged_oac_hook_bad_dir", stage_dir / "handler.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    monkeypatch.setenv("OAC_GATEWAY_HOOKS_ENABLED", "1")
    asyncio.run(
        module.handle(
            "agent:start",
            {
                "platform": "telegram",
                "canonical_user_id": "ti",
                "message": "should fail open",
            },
        )
    )


def test_staged_gateway_hook_handler_context_cannot_override_event_type(tmp_path: Path) -> None:
    stage_dir = tmp_path / "staged-hook"
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(tmp_path / "artifacts"),
    )
    assert result.returncode == 0, result.stderr
    handler = (stage_dir / "handler.py").read_text(encoding="utf-8")
    assert "payload = {**dict(context), \"event_type\": event_type}" in handler


def test_gateway_hook_bundle_refuses_live_hermes_hooks_target_without_explicit_flag(tmp_path: Path) -> None:
    fake_home = tmp_path / ".hermes"
    live_target = fake_home / "hooks" / "oac-context"

    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(live_target),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        env={"HOME": str(tmp_path)},
    )

    assert result.returncode == 2
    assert "refusing to stage directly under ~/.hermes/hooks" in result.stderr
    assert not live_target.exists()


def test_gateway_hook_bundle_can_opt_into_live_target_but_still_stays_disabled(tmp_path: Path) -> None:
    fake_home = tmp_path / ".hermes"
    live_target = fake_home / "hooks" / "oac-context"

    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(live_target),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        "--allow-live-target",
        env={"HOME": str(tmp_path)},
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((live_target / "bundle-manifest.json").read_text(encoding="utf-8"))
    assert manifest["live_install"] is True
    assert manifest["enabled_env_var"] == "OAC_GATEWAY_HOOKS_ENABLED"
    assert "OAC_GATEWAY_HOOKS_ENABLED=1" in (live_target / "INSTALL.md").read_text(encoding="utf-8")


def stage_test_bundle(tmp_path: Path) -> Path:
    stage_dir = tmp_path / "staged-hook"
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        "--python",
        PYTHON,
        "--src-path",
        str(REPO_ROOT / "src"),
    )
    assert result.returncode == 0, result.stderr
    return stage_dir


def test_gateway_hook_install_plan_writes_exact_copy_plan_without_touching_target(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / ".hermes" / "hooks"
    plan_out = tmp_path / "install-plan.json"

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--plan-out",
        str(plan_out),
    )

    assert result.returncode == 0, result.stderr
    assert "Planned gateway hook install" in result.stdout
    assert not (hooks_root / "oac-context").exists()

    plan = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan["artifact_type"] == "gateway_hook_install_plan"
    assert plan["mode"] == "dry_run"
    assert plan["apply"] is False
    assert plan["hook_name"] == "oac-context"
    assert plan["target_dir"] == str(hooks_root / "oac-context")
    assert plan["enabled_after_install"] is False
    assert plan["delivery_action"] == "none"
    assert plan["gateway_restart_action"] == "none"
    assert plan["requires_env_enable"] == "OAC_GATEWAY_HOOKS_ENABLED=1"
    assert [item["relative_path"] for item in plan["files"]] == [
        "HOOK.yaml",
        "handler.py",
        "INSTALL.md",
        "bundle-manifest.json",
    ]
    assert all(len(item["sha256"]) == 64 for item in plan["files"])


def test_gateway_hook_install_apply_requires_explicit_confirmation(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / ".hermes" / "hooks"

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--apply",
    )

    assert result.returncode == 2
    assert "--confirm-hook-name oac-context" in result.stderr
    assert not (hooks_root / "oac-context").exists()


def test_gateway_hook_install_apply_copies_bundle_but_keeps_env_disabled(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / ".hermes" / "hooks"
    plan_out = tmp_path / "install-plan.json"

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--plan-out",
        str(plan_out),
        "--apply",
        "--confirm-hook-name",
        "oac-context",
    )

    assert result.returncode == 0, result.stderr
    assert "Installed gateway hook bundle" in result.stdout
    target_dir = hooks_root / "oac-context"
    assert target_dir.exists()
    for relative_path in ["HOOK.yaml", "handler.py", "INSTALL.md", "bundle-manifest.json"]:
        assert (target_dir / relative_path).read_text(encoding="utf-8") == (
            stage_dir / relative_path
        ).read_text(encoding="utf-8")

    plan = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan["mode"] == "apply"
    assert plan["apply"] is True
    assert plan["status"] == "installed"
    assert plan["enabled_after_install"] is False
    assert plan["requires_env_enable"] == "OAC_GATEWAY_HOOKS_ENABLED=1"
    assert plan["gateway_restart_action"] == "none"


def test_gateway_hook_install_apply_refuses_existing_target_without_force(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / ".hermes" / "hooks"
    target_dir = hooks_root / "oac-context"
    target_dir.mkdir(parents=True)
    marker = target_dir / "do-not-clobber.txt"
    marker.write_text("existing live hook", encoding="utf-8")

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--apply",
        "--confirm-hook-name",
        "oac-context",
    )

    assert result.returncode == 2
    assert "target already exists" in result.stderr
    assert marker.read_text(encoding="utf-8") == "existing live hook"
    assert not (target_dir / "handler.py").exists()


def test_gateway_hook_install_apply_force_replaces_existing_target_atomically(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / ".hermes" / "hooks"
    target_dir = hooks_root / "oac-context"
    target_dir.mkdir(parents=True)
    (target_dir / "old.txt").write_text("old hook", encoding="utf-8")

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--apply",
        "--confirm-hook-name",
        "oac-context",
        "--force",
    )

    assert result.returncode == 0, result.stderr
    assert not (target_dir / "old.txt").exists()
    assert (target_dir / "handler.py").exists()
    assert (target_dir / "HOOK.yaml").exists()


def test_gateway_hook_install_rejects_malformed_bundle(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    (stage_dir / "handler.py").unlink()

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(tmp_path / ".hermes" / "hooks"),
    )

    assert result.returncode == 2
    assert "missing required bundle file: handler.py" in result.stderr


def test_gateway_hook_install_dry_run_rejects_plan_output_inside_target(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / ".hermes" / "hooks"
    plan_out_inside_target = hooks_root / "oac-context" / "plan.json"

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--plan-out",
        str(plan_out_inside_target),
    )

    assert result.returncode == 2
    assert "plan-out must not be inside the hook target" in result.stderr
    assert not (hooks_root / "oac-context").exists()


def test_gateway_hook_install_rejects_unsafe_hook_names(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)

    for unsafe_name in ["../evil", "/tmp/evil", "a/b", ".", "..", "bad\nname", "bad:name"]:
        result = run_oac(
            "gateway-hook",
            "install",
            "--bundle-dir",
            str(stage_dir),
            "--hooks-root",
            str(tmp_path / ".hermes" / "hooks"),
            "--hook-name",
            unsafe_name,
        )
        assert result.returncode == 2, unsafe_name
        assert "unsafe hook name" in result.stderr


def test_gateway_hook_install_rejects_tampered_hook_yaml(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    (stage_dir / "HOOK.yaml").write_text(
        "name: oac-context\ndescription: tampered\nevents:\n  - agent:start\n  - command:*\n",
        encoding="utf-8",
    )

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(tmp_path / ".hermes" / "hooks"),
    )

    assert result.returncode == 2
    assert "HOOK.yaml must match staged bundle manifest" in result.stderr


def test_gateway_hook_install_rejects_tampered_handler(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    (stage_dir / "handler.py").write_text("import requests\n", encoding="utf-8")

    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(tmp_path / ".hermes" / "hooks"),
    )

    assert result.returncode == 2
    assert "handler.py must match staged bundle manifest" in result.stderr


def test_gateway_hook_install_preserves_custom_safe_enabled_env_var(tmp_path: Path) -> None:
    stage_dir = tmp_path / "staged-hook"
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        "--enabled-env-var",
        "OAC_TEST_HOOKS_ENABLED",
    )
    assert result.returncode == 0, result.stderr

    plan_out = tmp_path / "plan.json"
    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(tmp_path / ".hermes" / "hooks"),
        "--plan-out",
        str(plan_out),
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(plan_out.read_text(encoding="utf-8"))
    assert plan["requires_env_enable"] == "OAC_TEST_HOOKS_ENABLED=1"


def test_gateway_hook_bundle_rejects_unsafe_enabled_env_var(tmp_path: Path) -> None:
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(tmp_path / "staged-hook"),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(tmp_path / "artifacts"),
        "--enabled-env-var",
        "BAD-NAME",
    )

    assert result.returncode == 2
    assert "unsafe enabled env var" in result.stderr


def install_test_bundle(tmp_path: Path) -> tuple[Path, Path]:
    write_cross_channel_fixture(tmp_path / ".oac")
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / "fake-hermes-hooks"
    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--apply",
        "--confirm-hook-name",
        "oac-context",
    )
    assert result.returncode == 0, result.stderr
    return hooks_root, hooks_root / "oac-context"


def test_gateway_hook_smoke_runs_installed_handler_with_env_enabled(tmp_path: Path) -> None:
    hooks_root, _hook_dir = install_test_bundle(tmp_path)
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "live-smoke.json"
    write_agent_start_event(event_path)

    result = run_oac(
        "gateway-hook",
        "smoke",
        "--hooks-root",
        str(hooks_root),
        "--hook-name",
        "oac-context",
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        "--forbidden-string",
        "ORCHID-123",
    )

    assert result.returncode == 0, result.stderr
    assert "Smoked installed gateway hook" in result.stdout
    smoke = json.loads(out_path.read_text(encoding="utf-8"))
    rendered = json.dumps(smoke, sort_keys=True)
    assert "ORCHID-123" not in rendered
    assert smoke["artifact_type"] == "gateway_hook_live_smoke"
    assert smoke["status"] == "passed"
    assert smoke["enabled_env_var"] == "OAC_GATEWAY_HOOKS_ENABLED"
    assert smoke["enabled_during_smoke"] is True
    assert smoke["delivery_action"] == "none"
    assert smoke["gateway_restart_action"] == "none"
    assert smoke["context_artifacts_created"] == 1
    assert smoke["context_artifacts"][0]["smoke_id"] == smoke["smoke_id"]
    assert smoke["context_artifacts"][0]["status"] == "context_ready"
    assert smoke["context_artifacts"][0]["delivery_action"] == "none"


def test_gateway_hook_smoke_refuses_live_hermes_hooks_root_without_flag(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    live_hooks_root = fake_home / ".hermes" / "hooks"
    event_path = tmp_path / "agent-start.json"
    write_agent_start_event(event_path)

    result = run_oac(
        "gateway-hook",
        "smoke",
        "--hooks-root",
        str(live_hooks_root),
        "--hook-name",
        "oac-context",
        "--event",
        str(event_path),
        "--out",
        str(tmp_path / "live-smoke.json"),
        env={"HOME": str(fake_home)},
    )

    assert result.returncode == 2
    assert "refusing to smoke live Hermes hooks root" in result.stderr


def test_gateway_hook_smoke_fails_redaction_without_persisting_forbidden_string(tmp_path: Path) -> None:
    hooks_root, _hook_dir = install_test_bundle(tmp_path)
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "live-smoke.json"
    write_agent_start_event(event_path)

    result = run_oac(
        "gateway-hook",
        "smoke",
        "--hooks-root",
        str(hooks_root),
        "--hook-name",
        "oac-context",
        "--event",
        str(event_path),
        "--out",
        str(out_path),
        "--forbidden-string",
        "## Omnichannel Agent Continuity",
    )

    assert result.returncode == 1
    smoke_text = out_path.read_text(encoding="utf-8")
    assert "## Omnichannel Agent Continuity" not in smoke_text
    smoke = json.loads(smoke_text)
    assert smoke["status"] == "failed"
    assert smoke["checks"]["redaction_ok"] is False
    for artifact in smoke["context_artifacts"]:
        assert not Path(artifact["path"]).exists()


def test_gateway_hook_smoke_fails_when_context_artifact_is_not_ready(tmp_path: Path) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / "fake-hermes-hooks"
    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--apply",
        "--confirm-hook-name",
        "oac-context",
    )
    assert result.returncode == 0, result.stderr
    event_path = tmp_path / "agent-start.json"
    out_path = tmp_path / "live-smoke.json"
    write_agent_start_event(event_path)

    result = run_oac(
        "gateway-hook",
        "smoke",
        "--hooks-root",
        str(hooks_root),
        "--hook-name",
        "oac-context",
        "--event",
        str(event_path),
        "--out",
        str(out_path),
    )

    assert result.returncode == 1
    smoke = json.loads(out_path.read_text(encoding="utf-8"))
    assert smoke["status"] == "failed"
    assert smoke["checks"]["context_ready"] is False
    assert smoke["context_artifacts"][0]["status"] == "no_context"


def test_gateway_hook_smoke_rejects_unrelated_concurrent_context_ready_artifact(tmp_path: Path, monkeypatch) -> None:
    stage_dir = stage_test_bundle(tmp_path)
    hooks_root = tmp_path / "fake-hermes-hooks"
    result = run_oac(
        "gateway-hook",
        "install",
        "--bundle-dir",
        str(stage_dir),
        "--hooks-root",
        str(hooks_root),
        "--apply",
        "--confirm-hook-name",
        "oac-context",
    )
    assert result.returncode == 0, result.stderr
    event_path = tmp_path / "agent-start.json"
    write_agent_start_event(event_path)
    artifact_dir = tmp_path / "artifacts"

    class FakeModule:
        @staticmethod
        def handle(event_type: str, context: dict) -> None:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            (artifact_dir / "hook-context-unrelated.json").write_text(
                json.dumps(
                    {
                        "artifact_type": "gateway_hook_context",
                        "status": "context_ready",
                        "event_type": event_type,
                        "smoke_id": "not-this-smoke",
                        "context_markdown": "unrelated concurrent artifact",
                        "delivery_action": "none",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

    monkeypatch.setattr("oac.gateway_hook.load_hook_handler", lambda _path: FakeModule)

    passed, smoke = run_gateway_hook_smoke(
        hooks_root=hooks_root,
        hook_name="oac-context",
        event_path=event_path,
        out=tmp_path / "live-smoke.json",
    )

    assert passed is False
    assert smoke["status"] == "failed"
    assert smoke["checks"]["context_artifact_created"] is True
    assert smoke["checks"]["context_ready"] is False


def test_staged_gateway_hook_handler_kill_switch_false_values_do_no_work(tmp_path: Path, monkeypatch) -> None:
    stage_dir = tmp_path / "staged-hook"
    artifact_dir = tmp_path / "artifacts"
    result = run_oac(
        "gateway-hook",
        "bundle",
        "--out-dir",
        str(stage_dir),
        "--store",
        str(tmp_path / ".oac"),
        "--artifact-dir",
        str(artifact_dir),
    )
    assert result.returncode == 0, result.stderr
    spec = importlib.util.spec_from_file_location("staged_oac_hook_false_values", stage_dir / "handler.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for false_value in ["", " ", "0", "false", "no", "off"]:
        monkeypatch.setenv("OAC_GATEWAY_HOOKS_ENABLED", false_value)
        asyncio.run(module.handle("agent:start", {"platform": "telegram", "message": "disabled"}))

    assert not artifact_dir.exists()
