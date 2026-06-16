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


def test_alias_set_resolve_and_list_are_deterministic(tmp_path: Path) -> None:
    store = tmp_path / ".oac"

    set_result = run_oac(
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

    assert set_result.returncode == 0, set_result.stderr
    assert set_result.stdout == "Mapped telegram:thread-1340:Ti Kawamoto -> ti\n"

    state = json.loads((store / "state.json").read_text(encoding="utf-8"))
    assert state["identity_aliases"] == {"telegram:thread-1340:Ti Kawamoto": "ti"}

    resolve_result = run_oac(
        "alias",
        "resolve",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Ti Kawamoto",
    )
    assert resolve_result.returncode == 0, resolve_result.stderr
    assert resolve_result.stdout == "ti\n"

    missing_result = run_oac(
        "alias",
        "resolve",
        "--store",
        str(store),
        "--surface",
        "signal",
        "--channel-id",
        "dm-ti",
        "--sender",
        "Ti Kawamoto",
    )
    assert missing_result.returncode == 0, missing_result.stderr
    assert missing_result.stdout == ""

    list_result = run_oac("alias", "list", "--store", str(store))
    assert list_result.returncode == 0, list_result.stderr
    assert json.loads(list_result.stdout) == {"telegram:thread-1340:Ti Kawamoto": "ti"}


def test_context_can_resolve_canonical_user_from_identity_alias(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
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

    ti_record = run_oac(
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
        "Ti asked OAC to resolve identity aliases before gateway hooks.",
        "--topic-id",
        "oac-identity",
        "--topic-title",
        "OAC identity alias slice",
        "--sensitivity",
        "private",
        "--timestamp-ms",
        "6000",
    )
    assert ti_record.returncode == 0, ti_record.stderr

    stranger_record = run_oac(
        "record",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Somebody Else",
        "--canonical-user-id",
        "somebody-else",
        "--role",
        "user",
        "--summary",
        "Somebody else asked about an unrelated topic.",
        "--topic-id",
        "other-topic",
        "--topic-title",
        "Other topic",
        "--sensitivity",
        "public",
        "--timestamp-ms",
        "6100",
    )
    assert stranger_record.returncode == 0, stranger_record.stderr

    context_result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Ti Kawamoto",
        "--query",
        "next slice",
        "--as-of-ms",
        "7000",
    )

    assert context_result.returncode == 0, context_result.stderr
    assert "Canonical user: ti" in context_result.stdout
    assert "OAC identity alias slice" in context_result.stdout
    assert "Ti asked OAC to resolve identity aliases before gateway hooks." in context_result.stdout
    assert "Somebody else asked" not in context_result.stdout


def test_context_without_canonical_or_alias_fails_open(tmp_path: Path) -> None:
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
        "Ti asked OAC to resolve identity aliases before gateway hooks.",
        "--topic-id",
        "oac-identity",
        "--sensitivity",
        "private",
        "--timestamp-ms",
        "6000",
    )
    assert record_result.returncode == 0, record_result.stderr

    context_result = run_oac(
        "context",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "thread-1340",
        "--sender",
        "Ti Kawamoto",
        "--query",
        "next slice",
        "--as-of-ms",
        "7000",
    )

    assert context_result.returncode == 0
    assert context_result.stdout == ""
    assert context_result.stderr == ""
