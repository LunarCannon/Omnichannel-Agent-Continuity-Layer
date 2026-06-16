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


def record(store: Path, **overrides: str) -> None:
    values = {
        "surface": "telegram",
        "channel_id": "thread-1340",
        "sender": "Ti Kawamoto",
        "canonical_user_id": "ti",
        "role": "user",
        "summary": "Ti picked the context slice.",
        "topic_id": "oac-context",
        "topic_title": "OAC context command slice",
        "sensitivity": "private",
        "timestamp_ms": "4000",
    }
    values.update(overrides)
    args = [
        "record",
        "--store",
        str(store),
        "--surface",
        values["surface"],
        "--channel-id",
        values["channel_id"],
        "--sender",
        values["sender"],
        "--canonical-user-id",
        values["canonical_user_id"],
        "--role",
        values["role"],
        "--summary",
        values["summary"],
        "--topic-id",
        values["topic_id"],
        "--topic-title",
        values["topic_title"],
        "--sensitivity",
        values["sensitivity"],
        "--timestamp-ms",
        values["timestamp_ms"],
    ]
    for flag in ("decision", "question", "promise", "fact"):
        if flag in values:
            args.extend([f"--{flag}", values[flag]])
    result = run_oac(*args)
    assert result.returncode == 0, result.stderr


def test_context_emits_surface_filtered_brief_to_stdout_from_store(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    record(
        store,
        summary="Ti picked the context command as the next OAC slice.",
        decision="Implement context before gateway hooks.",
        question="What prompt wrapper should consume context?",
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
        "let's get the next slice done",
        "--as-of-ms",
        "5000",
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "Wrote" not in result.stdout
    assert "## Omnichannel Agent Continuity" in result.stdout
    assert "Surface policy: telegram / low / group" in result.stdout
    assert "Canonical user: ti" in result.stdout
    assert "Likely continuation: OAC context command slice" in result.stdout
    assert "Ti picked the context command as the next OAC slice." in result.stdout
    assert "### Contradictions" in result.stdout
    assert "### Decay" in result.stdout


def test_context_respects_max_chars_without_cutting_mid_line(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    record(store, summary="Ti picked the context command as the next OAC slice.")

    result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "context slice",
        "--as-of-ms",
        "5000",
        "--max-chars",
        "220",
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout) <= 220
    assert result.stdout.endswith("\n")
    assert "[truncated to fit max chars]" in result.stdout
    assert result.stdout.splitlines()[-1] == "[truncated to fit max chars]"


def test_context_redacts_sensitive_cross_channel_details_on_low_trust_surface(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    record(
        store,
        surface="signal",
        channel_id="dm-ti",
        summary="Fake sensitive note: pretend vault code is BANANA-123.",
        topic_id="oac-context",
        topic_title="OAC context command slice",
        sensitivity="secret",
        timestamp_ms="4100",
        fact="fake_secret=BANANA-123",
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
        "context slice",
        "--as-of-ms",
        "5000",
    )

    assert result.returncode == 0, result.stderr
    assert "BANANA-123" not in result.stdout
    assert "Sensitive context exists on a higher-trust surface; use Signal/local before acting." in result.stdout
    assert "Recent safe events\n- None" in result.stdout


def test_context_missing_store_fails_open_with_empty_stdout(tmp_path: Path) -> None:
    result = run_oac(
        "context",
        "--store",
        str(tmp_path / "missing"),
        "--surface",
        "telegram",
        "--canonical-user-id",
        "ti",
        "--query",
        "context slice",
        "--as-of-ms",
        "5000",
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
