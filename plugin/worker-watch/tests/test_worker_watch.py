"""Contract tests for workerwatch.worker_watch (running cards across all boards).

The read side of the Worker Watch dashboard plugin (ADR-0002): every board's
kanban.db is opened through the read-only URI (I8 pins this repo from writing);
a row per task whose status is 'running', carrying the same-origin chat-resume
link built by workerlink's chat_path.
"""

from __future__ import annotations

import json
import signal
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from workerwatch import worker_watch
from workerwatch.worker_watch import (
    board_dbs,
    connect_ro,
    graph_snapshot,
    recent_sessions,
    running_workers,
    watch_snapshot,
)

TABLES = """
CREATE TABLE tasks (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    assignee       TEXT,
    status         TEXT NOT NULL,
    started_at     INTEGER,
    current_run_id INTEGER
);
CREATE TABLE task_runs (
    id         INTEGER PRIMARY KEY,
    task_id    TEXT NOT NULL,
    profile    TEXT,
    started_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
"""

# The real board schema (parent -> child dependency edges, one row per edge).
LINKS = """
CREATE TABLE task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
"""

SCHEMA = TABLES + LINKS


def _board(
    home: Path,
    slug: str,
    tasks=(),
    runs=(),
    events=(),
    links=(),
    with_task_links: bool = True,
) -> Path:
    """Create kanban/boards/<slug>/kanban.db under home and fill it.

    `events` rows are ``(task_id, kind, payload)`` or
    ``(task_id, kind, payload, created_at)`` — the explicit time is what the
    history section sorts on. `links` rows are ``(parent_id, child_id)``;
    ``with_task_links=False`` builds a schema-drifted board with no link table.
    """
    db = home / "kanban" / "boards" / slug / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA if with_task_links else TABLES)
    con.executemany(
        "insert into tasks (id, title, assignee, status, started_at, current_run_id)"
        " values (?,?,?,?,?,?)",
        tasks,
    )
    con.executemany(
        "insert into task_runs (id, task_id, profile, started_at) values (?,?,?,?)", runs
    )
    con.executemany(
        "insert into task_events (task_id, kind, payload, created_at) values (?,?,?,?)",
        [e if len(e) == 4 else (*e, 1) for e in events],
    )
    if with_task_links:
        con.executemany("insert into task_links (parent_id, child_id) values (?,?)", list(links))
    con.commit()
    con.close()
    return db


def _fixture_home(tmp_path: Path) -> Path:
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_run", "running card", "default", "running", 100, 7),
            ("t_done", "finished card", "default", "done", 90, None),
        ],
        runs=[(7, "t_run", "default", 200)],
        events=[
            ("t_run", "owner_admitted", json.dumps({"session_id": "sess-1"})),
            ("t_done", "owner_admitted", json.dumps({"session_id": "sess-2"})),
        ],
    )
    return tmp_path


def test_board_dbs_skips_underscore_missing_db_and_stray_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _fixture_home(tmp_path)
    _board(tmp_path, "_archived", tasks=[("t_x", "x", None, "running", 1, None)])
    (tmp_path / "kanban" / "boards" / "emptyboard").mkdir(parents=True)
    (tmp_path / "kanban" / "boards" / "stray.txt").write_text("not a board")
    slugs = [slug for slug, _ in board_dbs(tmp_path)]
    assert slugs == ["devtools"]


def test_running_workers_returns_only_running_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rows = running_workers(_fixture_home(tmp_path))
    assert [(r["board"], r["task_id"]) for r in rows] == [("devtools", "t_run")]
    assert rows[0]["title"] == "running card"
    assert rows[0]["assignee"] == "default"


def test_running_workers_prefers_current_run_fields(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "b",
        tasks=[("t_a", "a", None, "running", 100, 9)],
        runs=[(9, "t_a", "coder", 200)],
    )
    row = running_workers(tmp_path)[0]
    assert row["run_started_at"] == 200
    assert row["profile"] == "coder"


def test_running_workers_falls_back_to_task_started_and_default_profile(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "b",
        tasks=[("t_a", "a", None, "running", 100, None)],
        events=[("t_a", "owner_admitted", json.dumps({"session_id": "s"}))],
    )
    row = running_workers(tmp_path)[0]
    assert row["run_started_at"] == 100
    assert row["profile"] == "default"
    assert row["chat_link"] == "/chat?profile=default&resume=s"


def test_chat_link_is_relative_same_origin(tmp_path) -> None:  # type: ignore[no-untyped-def]
    row = running_workers(_fixture_home(tmp_path))[0]
    assert row["session_id"] == "sess-1"
    assert row["chat_link"] == "/chat?profile=default&resume=sess-1"


def test_row_without_admitted_session_kept_with_none_link(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "b",
        tasks=[("t_a", "a", None, "running", 100, None)],
        events=[("t_a", "spawned", json.dumps({"pid": 1}))],
    )
    row = running_workers(tmp_path)[0]
    assert row["session_id"] is None
    assert row["chat_link"] is None


def test_running_workers_skips_corrupt_board(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _fixture_home(tmp_path)
    bad = tmp_path / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a database")
    rows = running_workers(tmp_path)
    assert [r["task_id"] for r in rows] == ["t_run"]


def test_running_workers_reads_hermes_home_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HERMES_HOME", str(_fixture_home(tmp_path)))
    rows = running_workers()
    assert [r["task_id"] for r in rows] == ["t_run"]


def test_rows_sorted_by_board_then_task(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(tmp_path, "beta", tasks=[("t_b", "b", None, "running", 1, None)])
    _board(
        tmp_path,
        "alpha",
        tasks=[("t_c", "c", None, "running", 1, None), ("t_a", "a", None, "running", 1, None)],
    )
    keys = [(r["board"], r["task_id"]) for r in running_workers(tmp_path)]
    assert keys == [("alpha", "t_a"), ("alpha", "t_c"), ("beta", "t_b")]


def test_connect_ro_rejects_writes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _board(tmp_path, "b", tasks=[("t_a", "a", None, "running", 1, None)])
    con = connect_ro(db)
    try:
        import pytest

        with pytest.raises(sqlite3.OperationalError):
            con.execute(
                "insert into task_events (task_id, kind, payload, created_at)"
                " values ('t_a','owned','{}',1)"
            )
    finally:
        con.close()


# --- recent sessions (the history section) ------------------------------------

# (task_id, title, status, session_id, session event time)
HISTORY = [
    ("t_done", "done card", "done", "s-done", 300),
    ("t_review", "review card", "review", "s-review", 400),
    ("t_blocked", "blocked card", "blocked", "s-blocked", 500),
]


def _history_home(tmp_path: Path) -> Path:
    """One board with a card per watched status, plus running/archived decoys."""
    _board(
        tmp_path,
        "devtools",
        tasks=[(tid, title, "default", status, 90, None) for tid, title, status, _, _ in HISTORY]
        + [
            ("t_archived", "archived card", "default", "archived", 93, None),
            ("t_run", "running card", "default", "running", 94, 7),
        ],
        runs=[(7, "t_run", "runner", 200)],
        events=[
            *(
                (tid, "owner_admitted", json.dumps({"session_id": sid}), when)
                for tid, _, _, sid, when in HISTORY
            ),
            ("t_archived", "owner_admitted", json.dumps({"session_id": "s-old"}), 900),
            ("t_run", "owner_admitted", json.dumps({"session_id": "s-run"}), 999),
        ],
    )
    return tmp_path


def test_recent_sessions_covers_done_review_blocked_newest_first(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rows = recent_sessions(_history_home(tmp_path))
    assert [(r["board"], r["task_id"]) for r in rows] == [
        ("devtools", "t_blocked"),
        ("devtools", "t_review"),
        ("devtools", "t_done"),
    ]


def test_recent_sessions_row_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    row = recent_sessions(_history_home(tmp_path))[0]
    assert row["title"] == "blocked card"
    assert row["status"] == "blocked"
    assert row["session_id"] == "s-blocked"
    assert row["session_at"] == 500
    assert row["chat_link"] == "/chat?profile=default&resume=s-blocked"


def test_recent_sessions_uses_newest_run_profile(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A finished card has no current_run_id: its profile comes from its last run."""
    _board(
        tmp_path,
        "b",
        tasks=[("t_done", "done", "researcher-a", "done", 100, None)],
        runs=[(3, "t_done", "reviewer", 150), (4, "t_done", "writer", 160)],
        events=[("t_done", "owner_admitted", json.dumps({"session_id": "s1"}), 170)],
    )
    row = recent_sessions(tmp_path)[0]
    assert row["profile"] == "writer"
    assert row["chat_link"] == "/chat?profile=writer&resume=s1"


def test_recent_sessions_skips_cards_without_a_usable_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_none", "no events", "default", "done", 100, None),
            ("t_empty", "empty session id", "default", "review", 100, None),
            ("t_bad", "unparsable payload", "default", "blocked", 100, None),
        ],
        events=[
            ("t_empty", "owner_admitted", json.dumps({"session_id": ""}), 200),
            ("t_empty", "spawned", json.dumps({"pid": 1}), 201),
            ("t_bad", "owner_admitted", "{broken", 210),
        ],
    )
    assert recent_sessions(tmp_path) == []


def test_recent_sessions_caps_at_the_default_of_25_newest_wins(tmp_path) -> None:  # type: ignore[no-untyped-def]
    tasks, events = [], []
    for i in range(30):
        tasks.append((f"t_{i:02d}", f"card {i}", "default", "done", 100, None))
        events.append(
            (f"t_{i:02d}", "owner_admitted", json.dumps({"session_id": f"s{i}"}), 100 + i)
        )
    _board(tmp_path, "b", tasks=tasks, events=events)
    rows = recent_sessions(tmp_path)
    assert len(rows) == 25
    assert [r["task_id"] for r in rows[:3]] == ["t_29", "t_28", "t_27"]
    assert rows[-1]["task_id"] == "t_05"


def test_recent_sessions_honours_an_explicit_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "b",
        tasks=[(f"t_{i}", "card", "default", "done", 100, None) for i in range(5)],
        events=[
            (f"t_{i}", "owner_admitted", json.dumps({"session_id": f"s{i}"}), 100 + i)
            for i in range(5)
        ],
    )
    assert [r["task_id"] for r in recent_sessions(tmp_path, limit=2)] == ["t_4", "t_3"]


def test_recent_sessions_skips_corrupt_board(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _history_home(tmp_path)
    bad = home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a database")
    assert [r["task_id"] for r in recent_sessions(home)] == ["t_blocked", "t_review", "t_done"]


def test_recent_sessions_reads_hermes_home_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HERMES_HOME", str(_history_home(tmp_path)))
    assert [r["task_id"] for r in recent_sessions()] == ["t_blocked", "t_review", "t_done"]


def test_recent_sessions_sorted_newest_across_boards(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "beta",
        tasks=[("t_b", "b", None, "done", 1, None)],
        events=[("t_b", "owner_admitted", json.dumps({"session_id": "sb"}), 500)],
    )
    _board(
        tmp_path,
        "alpha",
        tasks=[("t_a", "a", None, "done", 1, None), ("t_c", "c", None, "review", 1, None)],
        events=[
            ("t_a", "owner_admitted", json.dumps({"session_id": "sa"}), 700),
            ("t_c", "owner_admitted", json.dumps({"session_id": "sc"}), 500),
        ],
    )
    assert [(r["board"], r["task_id"]) for r in recent_sessions(tmp_path)] == [
        ("alpha", "t_a"),
        ("alpha", "t_c"),
        ("beta", "t_b"),
    ]


def test_recent_sessions_time_ties_break_on_board_then_task(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "beta",
        tasks=[("t_b", "b", None, "done", 1, None)],
        events=[("t_b", "owner_admitted", json.dumps({"session_id": "sb"}), 500)],
    )
    _board(
        tmp_path,
        "alpha",
        tasks=[("t_z", "z", None, "done", 1, None), ("t_a", "a", None, "done", 1, None)],
        events=[
            ("t_z", "owner_admitted", json.dumps({"session_id": "sz"}), 500),
            ("t_a", "owner_admitted", json.dumps({"session_id": "sa"}), 500),
        ],
    )
    assert [(r["board"], r["task_id"]) for r in recent_sessions(tmp_path)] == [
        ("alpha", "t_a"),
        ("alpha", "t_z"),
        ("beta", "t_b"),
    ]


def test_recent_sessions_survives_a_non_integer_event_time(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Schema drift (created_at stored as text) must not poison the sort or the UI."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_bad", "text time", "default", "done", 100, None),
            ("t_good", "int time", "default", "done", 100, None),
        ],
        events=[
            ("t_bad", "owner_admitted", json.dumps({"session_id": "s-bad"}), "not-a-time"),
            ("t_good", "owner_admitted", json.dumps({"session_id": "s-good"}), 200),
        ],
    )
    rows = recent_sessions(tmp_path)
    assert [r["task_id"] for r in rows] == ["t_good", "t_bad"]
    assert rows[1]["session_at"] is None
    assert rows[1]["chat_link"] == "/chat?profile=default&resume=s-bad"


def test_running_rows_share_the_history_row_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One row shape for both sections, so the bundle needs one table component."""
    home = _history_home(tmp_path)
    run_row = running_workers(home)[0]
    assert run_row["status"] == "running"
    assert run_row["session_at"] == 999
    assert set(run_row) == set(recent_sessions(home)[0])


# --- the parent-child tree (task_links) --------------------------------------
#
# A real 2-level chain: root -> child -> grandchild. `task_links` holds one row
# per (parent, child) edge, so the read side has to walk it and hand the UI a
# `parent_id` plus a `depth` it can indent by.


def _chain_home(tmp_path: Path) -> Path:
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_root", "parent card", "default", "running", 100, None),
            ("t_mid", "child card", "default", "running", 101, None),
            ("t_leaf", "grandchild card", "default", "running", 102, None),
            ("t_alone", "unlinked card", "default", "running", 103, None),
        ],
        links=[("t_root", "t_mid"), ("t_mid", "t_leaf")],
    )
    return tmp_path


def test_rows_carry_parent_id_and_depth(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rows = {r["task_id"]: r for r in running_workers(_chain_home(tmp_path))}
    assert [(rows[t]["parent_id"], rows[t]["depth"]) for t in ("t_root", "t_mid", "t_leaf")] == [
        (None, 0),
        ("t_root", 1),
        ("t_mid", 2),
    ]
    assert (rows["t_alone"]["parent_id"], rows["t_alone"]["depth"]) == (None, 0)


def test_history_rows_carry_parent_id_and_depth_too(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One row shape, so the tree fields appear in the history section as well —
    this is the shape a real 2-level chain of finished cards arrives as."""
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_root", "parent card", "default", "done", 100, None),
            ("t_mid", "child card", "default", "done", 101, None),
            ("t_leaf", "grandchild card", "default", "done", 102, None),
        ],
        events=[
            (tid, "owner_admitted", json.dumps({"session_id": f"s-{tid}"}), 500 + i)
            for i, tid in enumerate(("t_root", "t_mid", "t_leaf"))
        ],
        links=[("t_root", "t_mid"), ("t_mid", "t_leaf")],
    )
    rows = {r["task_id"]: r for r in recent_sessions(tmp_path)}
    assert [(rows[t]["parent_id"], rows[t]["depth"]) for t in ("t_root", "t_mid", "t_leaf")] == [
        (None, 0),
        ("t_root", 1),
        ("t_mid", 2),
    ]


def test_depth_counts_a_parent_that_is_not_in_the_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Depth is the card's real place in the board's tree, not its index in a
    filtered section: a running child of a done parent still indents at 1."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_parent", "done parent", "default", "done", 100, None),
            ("t_child", "running child", "default", "running", 101, None),
            ("t_grand", "running grandchild", "default", "running", 102, None),
        ],
        links=[("t_parent", "t_child"), ("t_child", "t_grand")],
    )
    rows = {r["task_id"]: r for r in running_workers(tmp_path)}
    assert [(rows[t]["parent_id"], rows[t]["depth"]) for t in ("t_child", "t_grand")] == [
        ("t_parent", 1),
        ("t_child", 2),
    ]


def test_a_board_without_a_task_links_table_still_lists_rows(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Schema drift (an old board DB) must not blank the board: it reads as
    a board with no links."""
    _board(
        tmp_path,
        "old",
        tasks=[("t_a", "a", "default", "running", 100, None)],
        with_task_links=False,
    )
    row = running_workers(tmp_path)[0]
    assert (row["parent_id"], row["depth"]) == (None, 0)


def test_a_self_link_is_not_a_parent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    _board(
        tmp_path,
        "b",
        tasks=[("t_a", "a", "default", "running", 100, None)],
        links=[("t_a", "t_a")],
    )
    row = running_workers(tmp_path)[0]
    assert (row["parent_id"], row["depth"]) == (None, 0)


@contextmanager
def _terminates_within(seconds: float) -> Generator[None, None, None]:
    """Fail the test if the wrapped call outlives `seconds`.

    A link cycle with no chain guard spins; a hung gate is far worse to
    diagnose than a red test. SIGALRM is the cheapest watchdog on Linux.
    """

    def _fire(_sig, _frame):
        raise AssertionError(f"call did not terminate within {seconds}s")

    if not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_a_link_cycle_lists_every_row_with_a_finite_depth(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A corrupt link graph must not hang the scan or drop cards."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_a", "a", "default", "running", 100, None),
            ("t_b", "b", "default", "running", 101, None),
            ("t_c", "c", "default", "running", 102, None),
        ],
        links=[("t_a", "t_b"), ("t_b", "t_a"), ("t_b", "t_c")],
    )
    with _terminates_within(5.0):
        rows = {r["task_id"]: r for r in running_workers(tmp_path)}
    assert set(rows) == {"t_a", "t_b", "t_c"}
    assert all(isinstance(r["depth"], int) and 0 <= r["depth"] <= 3 for r in rows.values())


def test_a_card_with_two_parents_gets_the_lowest_parent_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Multi-parent cards exist on this host; one row needs one place in the
    tree, so the parent is the lowest task id — deterministic across scans."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_p2", "p2", "default", "running", 100, None),
            ("t_p1", "p1", "default", "running", 101, None),
            ("t_c", "child", "default", "running", 102, None),
        ],
        links=[("t_p2", "t_c"), ("t_p1", "t_c")],
    )
    row = {r["task_id"]: r for r in running_workers(tmp_path)}["t_c"]
    assert (row["parent_id"], row["depth"]) == ("t_p1", 1)


# --- the filter params (board / status / assignee) ---------------------------


def _filter_home(tmp_path: Path) -> Path:
    """Two boards, three assignees, one card per watched status."""
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_run_a", "running by alice", "alice", "running", 1, None),
            ("t_run_b", "running by bob", "bob", "running", 2, None),
            ("t_run_n", "running unassigned", None, "running", 3, None),
            ("t_done_a", "done by alice", "alice", "done", 4, None),
            ("t_review_b", "review by bob", "bob", "review", 5, None),
            ("t_blocked_a", "blocked by alice", "alice", "blocked", 6, None),
        ],
        events=[
            (tid, "owner_admitted", json.dumps({"session_id": f"s-{tid}"}), 100 + i)
            for i, tid in enumerate(("t_done_a", "t_review_b", "t_blocked_a"))
        ],
    )
    _board(
        tmp_path,
        "renderpool",
        tasks=[
            ("t_run_c", "other board running", "alice", "running", 7, None),
            ("t_done_r", "done by carol", "carol", "done", 8, None),
        ],
        events=[("t_done_r", "owner_admitted", json.dumps({"session_id": "s-r"}), 900)],
    )
    return tmp_path


def test_running_workers_filters_by_board(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _filter_home(tmp_path)
    assert [(r["board"], r["task_id"]) for r in running_workers(home, board="renderpool")] == [
        ("renderpool", "t_run_c")
    ]
    assert [r["task_id"] for r in running_workers(home, board="nope")] == []


def test_running_workers_filters_by_assignee(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _filter_home(tmp_path)
    assert [r["task_id"] for r in running_workers(home, assignee="bob")] == ["t_run_b"]
    # an unassigned card only shows in the unfiltered scan
    assert "t_run_n" in [r["task_id"] for r in running_workers(home)]
    assert "t_run_n" not in [r["task_id"] for r in running_workers(home, assignee="alice")]


def test_recent_sessions_filters_statuses(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _filter_home(tmp_path)
    assert [
        (r["task_id"], r["status"]) for r in recent_sessions(home, statuses=("review", "blocked"))
    ] == [("t_blocked_a", "blocked"), ("t_review_b", "review")]
    assert [r["task_id"] for r in recent_sessions(home, statuses=("done",))] == [
        "t_done_r",
        "t_done_a",
    ]
    assert recent_sessions(home, statuses=()) == []


def test_an_empty_status_selection_never_opens_a_board(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The empty selection short-circuits before the scan — an unchecked set of
    checkboxes must not read a single board DB."""
    seen: list[Path] = []
    monkeypatch.setattr(worker_watch, "board_dbs", lambda home: seen.append(home) or [])
    assert recent_sessions(tmp_path, statuses=()) == []
    assert seen == []


def test_recent_sessions_filters_board_and_assignee(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _filter_home(tmp_path)
    assert [r["task_id"] for r in recent_sessions(home, board="renderpool")] == ["t_done_r"]
    assert [r["task_id"] for r in recent_sessions(home, assignee="alice")] == [
        "t_blocked_a",
        "t_done_a",
    ]
    # the filters combine: board narrows to a board with no alice card
    assert recent_sessions(home, board="renderpool", assignee="alice") == []


def test_filters_never_break_the_row_shape_or_the_corrupt_board_skip(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A filtered scan is the same scan: same rows, same corrupt-board skip."""
    home = _filter_home(tmp_path)
    bad = home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    rows = running_workers(home, board="devtools", assignee="alice")
    assert [r["task_id"] for r in rows] == ["t_run_a"]
    assert {"parent_id", "depth"} <= set(rows[0])


# --- the per-board graph (what /graph serves) -------------------------------
#
# The tree columns above flatten `task_links` to one parent per card, which is
# what a table row needs and nothing more. The graph view reads the edges
# themselves, so a card with two parents keeps both edges and a card's depth is
# its shortest distance from a root. `graph_snapshot()` hands the bundle that:
# one entry per board, each with `nodes` and `edges`.


def _graph_home(tmp_path: Path) -> Path:
    """devtools: a root with three children, one child with a child of its own,
    one card in no link at all, and one archived decoy. renderpool: a single
    unlinked card. Matches the live board's shape: `t_4d496721` with three
    children and a grandchild under one of them."""
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_root", "bootstrap card", "default", "done", 100, None),
            ("t_c1", "first child", "default", "done", 101, None),
            ("t_c2", "second child", "default", "review", 102, None),
            ("t_c3", "third child", "default", "running", 103, 7),
            ("t_gchild", "grandchild card", "default", "done", 104, None),
            ("t_alone", "unlinked card", "default", "done", 105, None),
            ("t_arch", "archived card", "default", "archived", 106, None),
        ],
        runs=[(7, "t_c3", "default", 200)],
        events=[
            (tid, "owner_admitted", json.dumps({"session_id": f"s-{tid}"}), 300 + i)
            for i, tid in enumerate(
                ("t_root", "t_c1", "t_c2", "t_c3", "t_gchild", "t_alone", "t_arch")
            )
        ],
        links=[
            ("t_root", "t_c1"),
            ("t_root", "t_c2"),
            ("t_root", "t_c3"),
            ("t_c2", "t_gchild"),
        ],
    )
    _board(tmp_path, "renderpool", tasks=[("t_z", "other board card", None, "running", 50, None)])
    return tmp_path


def test_graph_snapshot_serves_nodes_and_edges_per_board(tmp_path) -> None:  # type: ignore[no-untyped-def]
    snap = graph_snapshot(_graph_home(tmp_path), board="devtools")
    assert snap["filters"] == {"board": "devtools"}
    assert [g["board"] for g in snap["boards"]] == ["devtools"]
    graph = snap["boards"][0]
    assert [(n["id"], n["depth"]) for n in graph["nodes"]] == [
        ("t_alone", 0),
        ("t_arch", 0),
        ("t_root", 0),
        ("t_c1", 1),
        ("t_c2", 1),
        ("t_c3", 1),
        ("t_gchild", 2),
    ]
    assert graph["edges"] == [
        {"parent": "t_c2", "child": "t_gchild"},
        {"parent": "t_root", "child": "t_c1"},
        {"parent": "t_root", "child": "t_c2"},
        {"parent": "t_root", "child": "t_c3"},
    ]


def test_graph_node_carries_title_status_session_and_resume_link(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The node fields the SVG draws: id, short title, status (which drives the
    dot colour), session id, the same same-origin resume link a table row
    carries, and the session's token counts (ADR-0010 — resolved from the
    session store by ``graph_snapshot``, ``None`` without one)."""
    graph = graph_snapshot(_graph_home(tmp_path), board="devtools")["boards"][0]
    node = next(n for n in graph["nodes"] if n["id"] == "t_c3")
    assert set(node) == {
        "id",
        "title",
        "status",
        "depth",
        "session_id",
        "profile",
        "chat_link",
        "tokens_in",
        "tokens_out",
        "tokens_cache_read",
        "tokens_cache_write",
        "tokens_reasoning",
    }
    assert node["title"] == "third child"
    assert node["status"] == "running"
    assert node["session_id"] == "s-t_c3"
    assert node["profile"] == "default"
    assert node["chat_link"] == "/chat?profile=default&resume=s-t_c3"


def test_graph_keeps_every_parent_edge_and_depths_from_the_nearest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A card with two parents keeps BOTH edges (unlike the table row's one
    parent), and its layer counts the parent that reaches it first — the deep
    branch must not push it two layers down."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_p1", "shallow parent", "default", "done", 100, None),
            ("t_p2", "deep parent", "default", "done", 101, None),
            ("t_mid", "between", "default", "done", 102, None),
            ("t_c", "two parents", "default", "done", 103, None),
        ],
        links=[("t_p1", "t_c"), ("t_p1", "t_mid"), ("t_mid", "t_p2"), ("t_p2", "t_c")],
    )
    graph = graph_snapshot(tmp_path, board="b")["boards"][0]
    assert {n["id"]: n["depth"] for n in graph["nodes"]} == {
        "t_p1": 0,
        "t_mid": 1,
        "t_p2": 2,
        "t_c": 1,
    }
    assert graph["edges"] == [
        {"parent": "t_mid", "child": "t_p2"},
        {"parent": "t_p1", "child": "t_c"},
        {"parent": "t_p1", "child": "t_mid"},
        {"parent": "t_p2", "child": "t_c"},
    ]


def test_graph_terminates_on_a_link_cycle_and_lists_every_card(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A corrupt link graph must not hang the request or drop cards: the BFS
    visited set cuts the cycle, and the seeded second pass places the cards no
    root can reach."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_a", "a", "default", "done", 100, None),
            ("t_b", "b", "default", "done", 101, None),
            ("t_c", "c", "default", "done", 102, None),
            ("t_top", "root above the cycle", "default", "done", 103, None),
        ],
        links=[("t_a", "t_b"), ("t_b", "t_a"), ("t_b", "t_c"), ("t_c", "t_a")],
    )
    with _terminates_within(5.0):
        graph = graph_snapshot(tmp_path, board="b")["boards"][0]
    assert sorted(n["id"] for n in graph["nodes"]) == ["t_a", "t_b", "t_c", "t_top"]
    # exact layers: a range check would still pass with every cycle card at 0
    assert {n["id"]: n["depth"] for n in graph["nodes"]} == {
        "t_top": 0,
        "t_a": 0,
        "t_b": 1,
        "t_c": 2,
    }
    assert graph["edges"] == [
        {"parent": "t_a", "child": "t_b"},
        {"parent": "t_b", "child": "t_a"},
        {"parent": "t_b", "child": "t_c"},
        {"parent": "t_c", "child": "t_a"},
    ]


def test_a_card_in_no_link_is_a_node_with_no_edges(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Orphan handling: the card is in `nodes` once, at layer 0, and appears in
    no edge — which is what the bundle uses to drop it into the unlinked row
    instead of drawing it into the tree."""
    graph = graph_snapshot(_graph_home(tmp_path), board="devtools")["boards"][0]
    alone = [n for n in graph["nodes"] if n["id"] == "t_alone"]
    assert len(alone) == 1 and alone[0]["depth"] == 0
    linked = {e["parent"] for e in graph["edges"]} | {e["child"] for e in graph["edges"]}
    assert "t_alone" not in linked


def test_a_board_without_a_task_links_table_graphs_as_no_links(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Schema drift (an old board DB) is a board with no edges, not a board that
    disappears from the payload."""
    _board(
        tmp_path,
        "old",
        tasks=[("t_a", "a", "default", "running", 100, None)],
        with_task_links=False,
    )
    graph = graph_snapshot(tmp_path, board="old")["boards"][0]
    assert [(n["id"], n["depth"]) for n in graph["nodes"]] == [("t_a", 0)]
    assert graph["edges"] == []


def test_an_edge_to_a_card_that_is_gone_is_dropped(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`task_links` can outlive a card. An edge pointing at a missing id has no
    node to draw, so it is left out rather than rendered to nowhere."""
    _board(
        tmp_path,
        "b",
        tasks=[("t_a", "a", "default", "done", 100, None)],
        links=[("t_gone", "t_a"), ("t_a", "t_also_gone")],
    )
    graph = graph_snapshot(tmp_path, board="b")["boards"][0]
    assert graph["edges"] == []
    assert [n["id"] for n in graph["nodes"]] == ["t_a"]


def test_a_task_links_row_with_a_null_id_does_not_take_down_the_payload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """One drifted board must not 500 the whole graph read. `task_links` on a DB
    that predates the NOT NULL columns can hold a NULL id, and sorting a mixed
    set raises a TypeError that `sqlite3.Error` does not catch — so the unusable
    rows are dropped and the boards around them still answer."""
    drifted = _board(
        tmp_path,
        "drift",
        tasks=[("t_a", "a", None, "done", 1, None), ("t_b", "b", None, "done", 1, None)],
        with_task_links=False,
    )
    setup = sqlite3.connect(drifted)  # fixture setup only; production reads stay mode=ro
    setup.execute("create table task_links (parent_id, child_id)")
    setup.executemany(
        "insert into task_links values (?,?)",
        [(None, "t_a"), ("t_b", None), ("t_b", "t_a")],
    )
    setup.commit()
    setup.close()
    _graph_home(tmp_path)
    snapshot = graph_snapshot(tmp_path)
    assert [b["board"] for b in snapshot["boards"]] == ["devtools", "drift", "renderpool"]
    entry = [b for b in snapshot["boards"] if b["board"] == "drift"][0]
    assert entry["edges"] == [{"parent": "t_b", "child": "t_a"}]
    assert sorted(n["id"] for n in entry["nodes"]) == ["t_a", "t_b"]


def test_graph_snapshot_lists_every_board_with_cards(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """No `board` filter means one entry per board that has any card, sorted by
    slug; the empty board and a corrupt DB get nothing rendered."""
    home = _graph_home(tmp_path)
    _board(tmp_path, "empty")
    bad = home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    snap = graph_snapshot(home)
    assert [g["board"] for g in snap["boards"]] == ["devtools", "renderpool"]
    assert snap["filters"] == {"board": None}
    assert graph_snapshot(home, board="nope")["boards"] == []


def test_graph_snapshot_reads_hermes_home_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("HERMES_HOME", str(_graph_home(tmp_path)))
    assert [g["board"] for g in graph_snapshot()["boards"]] == ["devtools", "renderpool"]


# --- the grouped snapshot (what the API serves) ------------------------------


def _tree_home(tmp_path: Path) -> Path:
    """devtools: a 2-level chain of done cards, one running child, one unrelated
    recent card. renderpool: a running card and nothing in the history."""
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_root", "parent card", "default", "done", 100, None),
            ("t_mid", "child card", "default", "done", 101, None),
            ("t_leaf", "grandchild card", "default", "done", 102, None),
            ("t_new", "unrelated card", "default", "review", 103, None),
            ("t_run", "running child of t_root", "default", "running", 104, 7),
        ],
        runs=[(7, "t_run", "runner", 200)],
        events=[
            (tid, "owner_admitted", json.dumps({"session_id": f"s-{tid}"}), when)
            for tid, when in (
                ("t_root", 500),
                ("t_mid", 510),
                ("t_leaf", 520),
                ("t_new", 900),
                ("t_run", 999),
            )
        ],
        links=[("t_root", "t_mid"), ("t_mid", "t_leaf"), ("t_root", "t_run")],
    )
    _board(
        tmp_path,
        "renderpool",
        tasks=[("t_z", "other board card", None, "running", 50, None)],
    )
    return tmp_path


def test_watch_snapshot_groups_each_section_by_board(tmp_path) -> None:  # type: ignore[no-untyped-def]
    snap = watch_snapshot(_tree_home(tmp_path))
    assert [
        (g["board"], [r["task_id"] for r in g["running"]], [r["task_id"] for r in g["recent"]])
        for g in snap["boards"]
    ] == [
        (
            "devtools",
            ["t_run"],
            ["t_new", "t_root", "t_mid", "t_leaf"],
        ),
        ("renderpool", ["t_z"], []),
    ]


def test_watch_snapshot_nests_children_under_their_parents(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Roots first (newest session first), each child right under its parent,
    indented by its real depth — a parent newer than its children does not
    float to the top of the group."""
    recent = watch_snapshot(_tree_home(tmp_path))["boards"][0]["recent"]
    assert [(r["task_id"], r["depth"]) for r in recent] == [
        ("t_new", 0),
        ("t_root", 0),
        ("t_mid", 1),
        ("t_leaf", 2),
    ]


def test_watch_snapshot_keeps_the_tree_inside_the_running_section(tmp_path) -> None:  # type: ignore[no-untyped-def]
    running = watch_snapshot(_tree_home(tmp_path))["boards"][0]["running"]
    assert [(r["task_id"], r["parent_id"], r["depth"]) for r in running] == [("t_run", "t_root", 1)]


def test_watch_snapshot_never_loses_a_card_to_a_broken_tree(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A cycle in task_links has no root to hang it on; the cards must still be
    listed, once each."""
    _board(
        tmp_path,
        "b",
        tasks=[
            ("t_a", "a", "default", "done", 100, None),
            ("t_b", "b", "default", "done", 101, None),
        ],
        events=[
            (tid, "owner_admitted", json.dumps({"session_id": f"s-{tid}"}), 100 + i)
            for i, tid in enumerate(("t_a", "t_b"))
        ],
        links=[("t_a", "t_b"), ("t_b", "t_a")],
    )
    with _terminates_within(5.0):
        rows = [r["task_id"] for g in watch_snapshot(tmp_path)["boards"] for r in g["recent"]]
    assert sorted(rows) == ["t_a", "t_b"]


def test_watch_snapshot_honours_the_board_status_and_assignee_filters(
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    home = _filter_home(tmp_path)
    snap = watch_snapshot(home, board="devtools", assignee="alice", statuses=("running", "done"))
    assert [g["board"] for g in snap["boards"]] == ["devtools"]
    assert [r["task_id"] for r in snap["boards"][0]["running"]] == ["t_run_a"]
    assert [r["task_id"] for r in snap["boards"][0]["recent"]] == ["t_done_a"]
    assert snap["filters"] == {
        "board": "devtools",
        "assignee": "alice",
        "statuses": ["running", "done"],
    }


def test_watch_snapshot_drops_a_section_whose_status_is_unchecked(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _filter_home(tmp_path)
    snap = watch_snapshot(home, statuses=("done",))
    assert [r["status"] for g in snap["boards"] for r in g["running"]] == []
    # grouping is by board first: newest-first is the order *inside* a group
    assert [r["task_id"] for g in snap["boards"] for r in g["recent"]] == ["t_done_a", "t_done_r"]
    # nothing selected at all: no groups, while the bar still lists the options
    empty = watch_snapshot(home, statuses=())
    assert empty["boards"] == []
    assert empty["board_options"] == ["devtools", "renderpool"]


def test_watch_snapshot_lists_the_filter_options_unfiltered(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The bar keeps every board and assignee on the host even while a filter
    hides them, so the select never collapses to the filtered choice."""
    home = _filter_home(tmp_path)
    bad = home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    snap = watch_snapshot(home, board="devtools", assignee="alice")
    assert snap["board_options"] == ["devtools", "renderpool"]
    assert snap["assignee_options"] == ["alice", "bob", "carol"]


def test_watch_snapshot_caps_the_history_before_grouping(tmp_path) -> None:  # type: ignore[no-untyped-def]
    tasks, events = [], []
    for i in range(30):
        tasks.append((f"t_{i:02d}", f"card {i}", "default", "done", 100, None))
        events.append(
            (f"t_{i:02d}", "owner_admitted", json.dumps({"session_id": f"s{i}"}), 100 + i)
        )
    _board(tmp_path, "devtools", tasks=tasks, events=events)
    snap = watch_snapshot(tmp_path, limit=4)
    rows = [r["task_id"] for g in snap["boards"] for r in g["recent"]]
    assert rows == ["t_29", "t_28", "t_27", "t_26"]


def test_watch_snapshot_reads_hermes_home_env_and_skips_a_corrupt_board(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    home = _tree_home(tmp_path)
    bad = home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"this is not a database")
    monkeypatch.setenv("HERMES_HOME", str(home))
    snap = watch_snapshot()
    assert [g["board"] for g in snap["boards"]] == ["devtools", "renderpool"]
    assert snap["board_options"] == ["devtools", "renderpool"]
