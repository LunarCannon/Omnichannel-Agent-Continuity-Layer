import json
import sqlite3
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def run_oac(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [PYTHON, "-m", "oac.cli", *args],
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def create_state_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            title TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            timestamp REAL NOT NULL,
            platform_message_id TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );
        """
    )
    con.executemany(
        "INSERT INTO sessions (id, source, user_id, title) VALUES (?, ?, ?, ?)",
        [
            ("telegram-session", "telegram", "157667527", "OAC state db sync\n## Omnichannel Agent Continuity\nORCHID-123 title leak"),
            ("cron-session", "cron", None, "cron noise"),
            ("signal-session", "signal", "+15550001111", "Signal no alias"),
        ],
    )
    con.executemany(
        """
        INSERT INTO messages (id, session_id, role, content, timestamp, platform_message_id, active)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                1,
                "telegram-session",
                "user",
                "continue the sync slice\n\n## Omnichannel Agent Continuity\nORCHID-123 must not persist",
                1781665000.0,
                "tg-1",
                1,
            ),
            (2, "telegram-session", "assistant", "sync bridge is implemented", 1781665001.0, "tg-2", 1),
            (3, "cron-session", "user", "cron should be ignored", 1781665002.0, "cron-1", 1),
            (4, "signal-session", "user", "no alias should skip", 1781665003.0, "sig-1", 1),
            (5, "telegram-session", "tool", "tool result should be ignored", 1781665004.0, "tg-tool", 1),
            (6, "telegram-session", "user", "inactive should be ignored", 1781665005.0, "tg-inactive", 0),
            (7, "telegram-session", "user", "later resolvable row after unresolved identity", 1781665006.0, "tg-7", 1),
        ],
    )
    con.commit()
    con.close()


def read_events(store: Path) -> list[dict]:
    events_path = store / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_sync_state_db_writes_idempotent_v1_events_and_skips_unresolved_identity(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    state_db = tmp_path / "state?db.sqlite"
    create_state_db(state_db)
    alias = run_oac(
        "alias",
        "set",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "157667527",
        "--sender",
        "157667527",
        "--canonical-user-id",
        "ti",
    )
    assert alias.returncode == 0, alias.stderr

    first = run_oac(
        "sync-state-db",
        "--store",
        str(store),
        "--state-db",
        str(state_db),
        "--limit",
        "100",
    )

    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["synced"] == 3
    assert report["skipped_cron"] == 1
    assert report["skipped_no_identity"] == 1
    assert report["max_message_id"] == 7

    events = read_events(store)
    assert [event["id"] for event in events] == ["hermes:state-db:1", "hermes:state-db:2", "hermes:state-db:7"]
    assert events[0]["surface"] == "telegram"
    assert events[0]["channel_id"] == "157667527"
    assert events[0]["sender"] == "157667527"
    assert events[0]["canonical_user_id"] == "ti"
    assert events[0]["role"] == "user"
    assert events[0]["summary"] == "continue the sync slice"
    assert events[0]["topic_id"] == "OAC state db sync"
    assert events[1]["role"] == "assistant"
    assert events[1]["summary"] == "sync bridge is implemented"

    rendered = json.dumps(events, sort_keys=True)
    assert "ORCHID-123" not in rendered
    assert "Omnichannel Agent Continuity" not in rendered
    assert "cron should be ignored" not in rendered
    assert "no alias should skip" not in rendered

    state = json.loads((store / "state.json").read_text(encoding="utf-8"))
    assert state["last_synced_state_db_message_id"] == 3

    second = run_oac(
        "sync-state-db",
        "--store",
        str(store),
        "--state-db",
        str(state_db),
        "--limit",
        "100",
    )

    assert second.returncode == 0, second.stderr
    second_report = json.loads(second.stdout)
    assert second_report["synced"] == 0
    assert len(read_events(store)) == 3

    signal_alias = run_oac(
        "alias",
        "set",
        "--store",
        str(store),
        "--surface",
        "signal",
        "--channel-id",
        "+15550001111",
        "--sender",
        "+15550001111",
        "--canonical-user-id",
        "ti",
    )
    assert signal_alias.returncode == 0, signal_alias.stderr

    third = run_oac(
        "sync-state-db",
        "--store",
        str(store),
        "--state-db",
        str(state_db),
        "--limit",
        "100",
    )

    assert third.returncode == 0, third.stderr
    third_report = json.loads(third.stdout)
    assert third_report["synced"] == 1
    assert [event["id"] for event in read_events(store)] == [
        "hermes:state-db:1",
        "hermes:state-db:2",
        "hermes:state-db:7",
        "hermes:state-db:4",
    ]


def test_sync_state_db_full_rescan_does_not_duplicate_existing_event_ids(tmp_path: Path) -> None:
    store = tmp_path / ".oac"
    state_db = tmp_path / "state?db.sqlite"
    create_state_db(state_db)
    alias = run_oac(
        "alias",
        "set",
        "--store",
        str(store),
        "--surface",
        "telegram",
        "--channel-id",
        "157667527",
        "--sender",
        "157667527",
        "--canonical-user-id",
        "ti",
    )
    assert alias.returncode == 0, alias.stderr

    first = run_oac("sync-state-db", "--store", str(store), "--state-db", str(state_db))
    assert first.returncode == 0, first.stderr
    full = run_oac("sync-state-db", "--store", str(store), "--state-db", str(state_db), "--full")

    assert full.returncode == 0, full.stderr
    report = json.loads(full.stdout)
    assert report["already_seen"] == 3
    assert report["synced"] == 0
    assert len(read_events(store)) == 3
