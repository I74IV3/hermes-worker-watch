"""The card-detail read behind the Worker Watch drawer (ADR-0012).

One card, fully: its facts and body, its newest events and comments, and the
admitted session's meta and token counts. Every store is opened read-only;
every answer is JSON-safe; a drifted board or store narrows the answer instead
of raising, and an unknown board or task reads as no card at all."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from workerwatch.card_detail import (
    BODY_CAP,
    COMMENT_LIMIT,
    EVENT_LIMIT,
    card_detail,
)
from workerwatch.root_chats import session_meta

TASKS = """
CREATE TABLE tasks (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    body               TEXT,
    assignee           TEXT,
    status             TEXT NOT NULL,
    block_kind         TEXT,
    last_failure_error TEXT,
    result             TEXT,
    created_at         INTEGER,
    started_at         INTEGER,
    completed_at       INTEGER,
    branch_name        TEXT,
    workspace_path     TEXT,
    session_id         TEXT
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT,
    body       TEXT,
    created_at INTEGER NOT NULL
);
"""

SESSIONS = """
CREATE TABLE sessions (
    id                        TEXT PRIMARY KEY,
    source                    TEXT NOT NULL,
    title                     TEXT,
    model                     TEXT,
    profile_name              TEXT,
    message_count             INTEGER,
    tool_call_count           INTEGER,
    last_activity_description TEXT,
    input_tokens              INTEGER,
    output_tokens             INTEGER,
    cache_read_tokens         INTEGER,
    cache_write_tokens        INTEGER,
    reasoning_tokens          INTEGER
);
"""


def _board(
    home: Path,
    slug: str,
    tasks=(),
    events=(),
    comments=(),
    full_schema: bool = True,
) -> Path:
    db = home / "kanban" / "boards" / slug / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    if full_schema:
        con.executescript(TASKS)
        con.executemany(
            "insert into tasks (id, title, body, assignee, status, block_kind,"
            " last_failure_error, result, created_at, started_at, completed_at,"
            " branch_name, workspace_path, session_id) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tasks,
        )
        con.executemany(
            "insert into task_events (task_id, kind, payload, created_at) values (?,?,?,?)",
            events,
        )
        con.executemany(
            "insert into task_comments (task_id, author, body, created_at) values (?,?,?,?)",
            comments,
        )
    else:
        # an old board slice: no body, no comments, no event payloads to trust
        con.executescript(
            "create table tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL,"
            " assignee TEXT, status TEXT NOT NULL);"
        )
        con.executemany(
            "insert into tasks (id, title, assignee, status) values (?,?,?,?)",
            [("t_old", "legacy card", "default", "done")],
        )
    con.commit()
    con.close()
    return db


def _store(home: Path) -> None:
    con = sqlite3.connect(home / "state.db")
    con.executescript(SESSIONS)
    con.execute(
        "insert into sessions (id, source, title, model, profile_name, message_count,"
        " tool_call_count, last_activity_description, input_tokens, output_tokens,"
        " cache_read_tokens, cache_write_tokens, reasoning_tokens)"
        " values ('s_w','kanban','worker run','qwen-max','default',42, 17,"
        " 'wrote the drift timer unit', 215800, 113296, 1200000, 30, 55)"
    )
    con.commit()
    con.close()


def _card(home: Path, board: str, task: str) -> dict:
    card = card_detail(board, task, home)
    assert card is not None
    return card


def _home(tmp_path: Path) -> Path:
    _board(
        tmp_path,
        "renderpool",
        tasks=[
            (
                "t_1",
                "Add drift timer units",
                "Install the systemd user units and the drift timer.\n\nDetails.",
                "default",
                "done",
                None,
                None,
                "units shipped, timer verified",
                100,
                110,
                120,
                "master",
                "/home/dan/renderpool",
                None,
            ),
            (
                "t_2",
                "blocked card",
                None,
                "default",
                "blocked",
                "empty_result",
                "boom",
                None,
                1,
                2,
                None,
                None,
                None,
                None,
            ),
        ],
        events=[
            ("t_1", "created", '{"status": "ready"}', 100),
            ("t_1", "owner_admitted", '{"session_id": "s_w"}', 105),
            ("t_1", "commented", '{"author": "default", "len": 21}', 110),
            ("t_1", "completed", '{"summary": "Fence verdict shipped in c441"}', 120),
            ("t_2", "created", '{"status": "ready"}', 100),
        ],
        comments=[
            ("t_1", "default", "worker chat session (click to open): http://x", 111),
            ("t_1", "default", "Implemented, gate green.", 121),
            ("t_1", "dan", "ship it", 122),
            ("t_1", "dan", "one more note", 123),
        ],
    )
    _board(tmp_path, "empty", tasks=[])
    _store(tmp_path)
    return tmp_path


def test_a_card_comes_back_with_facts_body_and_activity(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    card = _card(home, "renderpool", "t_1")
    assert card["board"] == "renderpool"
    assert card["id"] == "t_1"
    assert card["title"] == "Add drift timer units"
    assert "systemd user units" in card["body"]
    assert card["status"] == "done"
    assert card["result"] == "units shipped, timer verified"
    assert card["branch_name"] == "master"
    assert (card["created_at"], card["started_at"], card["completed_at"]) == (100, 110, 120)
    assert card["session_id"] == "s_w"
    # newest first, capped, and the event payload's human line comes along
    kinds = [e["kind"] for e in card["events"][:3]]
    assert kinds == ["completed", "commented", "owner_admitted"]
    assert card["events"][0]["detail"] == "Fence verdict shipped in c441"
    # and the newest comments too, newest first and capped
    assert COMMENT_LIMIT == 3
    assert [c["author"] for c in card["comments"]] == ["dan", "dan", "default"]
    assert card["comments"][0]["body"] == "one more note"


def test_the_admitted_session_rides_along(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    card = _card(home, "renderpool", "t_1")
    sess = card["session"]
    assert sess is not None
    assert sess["title"] == "worker run"
    assert sess["model"] == "qwen-max"
    # the one thing the drawer adds over the graph node: what it was doing
    assert sess["last_activity_description"] == "wrote the drift timer unit"
    assert (sess["message_count"], sess["tool_call_count"]) == (42, 17)
    assert sess["tokens_in"] == 215800
    assert sess["tokens_cache_read"] == 1200000
    assert sess["chat_link"] == "/chat?profile=default&resume=s_w"


def test_blocked_facts_show_why_a_card_stopped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    card = _card(home, "renderpool", "t_2")
    assert card["block_kind"] == "empty_result"
    assert card["last_failure_error"] == "boom"
    assert card["session"] is None  # t_2 never admitted a session


def test_a_long_body_is_capped_and_says_so(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path
    _board(
        home,
        "bigs",
        tasks=[
            (
                "t_long",
                "long",
                "x" * (BODY_CAP + 500),
                "default",
                "ready",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        ],
    )
    card = _card(home, "bigs", "t_long")
    assert len(card["body"]) == BODY_CAP
    assert card["body_truncated"] is True


def test_an_unknown_board_or_task_reads_as_no_card(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    assert card_detail("nosuch", "t_1", home) is None
    assert card_detail("renderpool", "t_nope", home) is None
    assert card_detail("renderpool", "", home) is None


def test_a_board_without_comments_or_events_boards_still_resolves(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path
    _board(home, "old", full_schema=False)
    card = _card(home, "old", "t_old")
    assert card["title"] == "legacy card"
    assert card["body"] is None
    assert card["events"] == []
    assert card["comments"] == []
    assert card["session"] is None


def test_the_drawer_resolves_the_session_exactly_like_the_resume_link(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # the tables join card→session through the newest owner_admitted event
    # (workerlink.latest_admitted), not the tasks.session_id column; a drawer
    # that read the column would show a session for cards whose rows carry no
    # resume link — I17's "a node and its row never disagree" extends to it
    home = _home(tmp_path)
    _board(
        home,
        "join",
        tasks=[
            (
                "t_col",
                "column only",
                None,
                "default",
                "running",
                None,
                None,
                None,
                1,
                2,
                None,
                None,
                None,
                "s_w",
            ),
            (
                "t_adm",
                "admitted",
                None,
                "default",
                "running",
                None,
                None,
                None,
                1,
                2,
                None,
                None,
                None,
                None,
            ),
        ],
        events=[("t_adm", "owner_admitted", '{"session_id": "s_w"}', 5)],
    )
    assert _card(home, "join", "t_col")["session"] is None
    admitted = _card(home, "join", "t_adm")
    assert admitted["session_id"] == "s_w"
    session = admitted["session"]
    assert session is not None and session["model"] == "qwen-max"


def test_a_missing_session_store_reads_as_no_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    (home / "state.db").unlink()
    card = _card(home, "renderpool", "t_1")
    assert card["session"] is None
    assert card["session_id"] == "s_w"  # the card still says which session it had


def test_event_caps_hold_on_a_busy_card(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = tmp_path
    _board(
        home,
        "busy",
        tasks=[
            (
                "t_b",
                "busy",
                None,
                "default",
                "running",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            )
        ],
        events=[
            ("t_b", "commented", json.dumps({"summary": f"note {i}"}), 100 + i) for i in range(20)
        ],
    )
    card = _card(home, "busy", "t_b")
    assert len(card["events"]) == EVENT_LIMIT
    assert card["events"][0]["detail"] == "note 19"


def test_drifted_cells_never_reach_the_encoder(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    db = home / "kanban" / "boards" / "renderpool" / "kanban.db"
    con = sqlite3.connect(db)
    con.execute(
        "update tasks set body = x'dead', branch_name = x'ab', created_at = 'text' where id = 't_1'"
    )
    con.execute("update task_events set payload = x'be' where kind = 'completed'")
    con.commit()
    con.close()
    card = _card(home, "renderpool", "t_1")
    assert card["body"] is None
    assert card["branch_name"] is None
    assert card["created_at"] is None
    # the unparsable payload keeps its kind and time but no detail line
    done = next(e for e in card["events"] if e["kind"] == "completed")
    assert done["detail"] is None
    json.dumps(card)  # the whole payload must encode


def test_both_stores_open_read_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    seen: list[str] = []
    real = sqlite3.connect

    def spy(*args, **kw):
        seen.append(str(args[0] if args else kw.get("uri", "")))
        return real(*args, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    assert card_detail("renderpool", "t_1", home) is not None
    assert seen, "no connection was opened"
    assert all(u.startswith("file:") and "mode=ro" in u for u in seen), seen


def test_session_meta_reads_the_store_read_only_and_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(tmp_path)
    meta = session_meta("s_w", home)
    assert meta is not None
    assert meta["model"] == "qwen-max"
    assert session_meta("s_missing", home) is None
    (home / "state.db").unlink()
    assert session_meta("s_w", home) is None
