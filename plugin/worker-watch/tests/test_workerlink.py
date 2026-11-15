"""Contract tests for workerwatch.workerlink (task -> dashboard chat-session link)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from workerwatch.workerlink import (
    board_db,
    chat_path,
    current_board,
    latest_admitted,
    latest_admitted_session_id,
    main,
    session_url,
)

SCHEMA = """
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
"""


def _db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    return _db_timed(tmp_path, [(*row, 1) for row in rows])


def _db_timed(tmp_path: Path, rows: list[tuple[str, str, str, int]]) -> Path:
    db = tmp_path / "kanban.db"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany(
        "insert into task_events (task_id, kind, payload, created_at) values (?,?,?,?)", rows
    )
    con.commit()
    con.close()
    return db


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def test_latest_admitted_returns_session_and_event_time(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The scan carries the event's created_at so history can be sorted by recency."""
    db = _db_timed(
        tmp_path,
        [
            ("t_a", "owner_admitted", json.dumps({"session_id": "old"}), 100),
            ("t_a", "spawned", json.dumps({"pid": 1}), 200),
            ("t_a", "owner_admitted", "{broken", 300),
            ("t_a", "owner_admitted", json.dumps({"session_id": "new"}), 400),
        ],
    )
    con = _ro(db)
    try:
        assert latest_admitted(con, "t_a") == ("new", 400)
    finally:
        con.close()


def test_latest_admitted_none_without_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(tmp_path, [("t_a", "spawned", json.dumps({"pid": 1}))])
    con = _ro(db)
    try:
        assert latest_admitted(con, "t_a") is None
    finally:
        con.close()


def test_session_url_uses_latest_owner_admitted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(
        tmp_path,
        [
            ("t_a", "owner_admitted", json.dumps({"session_id": "old"})),
            ("t_a", "worker_bound", json.dumps({"pid": 1})),
            ("t_a", "owner_admitted", json.dumps({"session_id": "new"})),
        ],
    )
    url = session_url(db, "t_a")
    assert url == "http://127.0.0.1:9119/chat?profile=default&resume=new"


def test_session_url_none_when_no_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(tmp_path, [("t_a", "spawned", json.dumps({"pid": 1}))])
    assert session_url(db, "t_a") is None


def test_session_url_skips_unparsable_payload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(
        tmp_path,
        [
            ("t_a", "owner_admitted", "{broken"),
            ("t_a", "owner_admitted", json.dumps({"session_id": "good"})),
        ],
    )
    assert "resume=good" in (session_url(db, "t_a") or "")


def test_session_url_missing_db_is_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert session_url(tmp_path / "nope.db", "t_a") is None


def test_board_db_path_under_hermes_home(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert board_db("renderpool") == tmp_path / "kanban" / "boards" / "renderpool" / "kanban.db"


def test_chat_path_is_relative_same_origin() -> None:
    assert chat_path("default", "sid") == "/chat?profile=default&resume=sid"
    assert chat_path("coder", "sid-2") == "/chat?profile=coder&resume=sid-2"


def test_latest_admitted_session_id_newest_wins(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(
        tmp_path,
        [
            ("t_a", "owner_admitted", json.dumps({"session_id": "old"})),
            ("t_a", "owner_admitted", json.dumps({"session_id": "new"})),
        ],
    )
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert latest_admitted_session_id(con, "t_a") == "new"
    finally:
        con.close()


def test_latest_admitted_session_id_none_without_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(tmp_path, [("t_a", "spawned", json.dumps({"pid": 1}))])
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert latest_admitted_session_id(con, "t_a") is None
    finally:
        con.close()


def test_latest_admitted_session_id_skips_non_dict_payloads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _db(
        tmp_path,
        [
            ("t_a", "owner_admitted", json.dumps({"session_id": "ok"})),
            ("t_a", "owner_admitted", json.dumps(["x"])),
            ("t_a", "owner_admitted", "null"),
        ],
    )
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert latest_admitted_session_id(con, "t_a") == "ok"
    finally:
        con.close()


def test_chat_path_percent_encodes_values() -> None:
    assert chat_path("a b", "s&x") == "/chat?profile=a%20b&resume=s%26x"


# --- the CLI side (ADR-0018 added these pins; main() predates them) ----------


def _home_with_board(tmp_path: Path, slug: str, events: list[tuple[str, str, str]]) -> Path:
    db = tmp_path / "kanban" / "boards" / slug / "kanban.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany(
        "insert into task_events (task_id, kind, payload, created_at) values (?,?,?,1)", events
    )
    con.commit()
    con.close()
    return tmp_path


def test_main_prints_the_url_and_exits_zero(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    home = _home_with_board(
        tmp_path,
        "solo",
        [("t_a", "owner_admitted", json.dumps({"session_id": "s1"}))],
    )
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert main(["t_a", "--board", "solo"]) == 0
    assert capsys.readouterr().out.strip() == (
        "http://127.0.0.1:9119/chat?profile=default&resume=s1"
    )


def test_main_without_a_worker_session_exits_one(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    home = _home_with_board(tmp_path, "solo", [("t_a", "spawned", json.dumps({"pid": 1}))])
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert main(["t_a", "--board", "solo"]) == 1
    assert "no worker session recorded for solo/t_a" in capsys.readouterr().err


def test_current_board_reads_the_pointer_or_defaults(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert current_board() == "default"  # no pointer file (true on this host too)
    pointer = tmp_path / "kanban" / "current"
    pointer.parent.mkdir(parents=True)
    pointer.write_text("solo\n", encoding="utf-8")
    assert current_board() == "solo"


def test_main_without_board_argv_uses_the_current_pointer(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch, capsys
) -> None:
    home = _home_with_board(
        tmp_path,
        "solo",
        [("t_a", "owner_admitted", json.dumps({"session_id": "s2"}))],
    )
    (home / "kanban" / "current").write_text("solo\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert main(["t_a"]) == 0
    assert "resume=s2" in capsys.readouterr().out
