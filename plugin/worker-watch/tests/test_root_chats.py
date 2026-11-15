"""Contract tests for workerwatch.root_chats — the user's own sessions on this host.

The read side of the Worker Watch "Root chats" section (ADR-0007): ``state.db``
is opened through a ``file:…?mode=ro`` URI (I8 pins this repo from writing), and
one row per root chat — a session with no parent, a human source, and not
archived or hidden — carries the same same-origin ``/chat`` resume link the card
rows carry (``workerlink.chat_path``).

The fixture builds the minimal slice of the real ``sessions`` table this read
needs (``id``, ``source``, ``parent_session_id``, ``title``, ``started_at``,
``last_activity_at``, ``archived``, ``hidden``, ``profile_name``) under
``tmp_path`` — the live ``~/.hermes/state.db`` is never opened by the suite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from workerwatch import root_chats as store
from workerwatch.root_chats import (
    ROOT_CHAT_CAP,
    ROOT_SOURCES,
    UNTITLED,
    connect_state_ro,
    root_chat_snapshot,
    root_chats,
    state_db,
    token_usage,
)

# The columns this read touches, a subset of the live schema.
SESSIONS = """
CREATE TABLE sessions (
    id                TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    parent_session_id TEXT,
    title             TEXT,
    started_at        REAL,
    last_activity_at  REAL,
    archived          INTEGER NOT NULL DEFAULT 0,
    hidden            INTEGER NOT NULL DEFAULT 0,
    pinned            INTEGER NOT NULL DEFAULT 0,
    profile_name      TEXT,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens  INTEGER NOT NULL DEFAULT 0
);
"""

# The same schema with the `pinned` column missing: the slice an older or
# drifted store keeps, which the read must still answer rows from.
SESSIONS_OLD = SESSIONS.replace("    pinned            INTEGER NOT NULL DEFAULT 0,\n", "")

ROW_KEYS = [
    "session_id",
    "title",
    "source",
    "profile",
    "pinned",
    "last_activity_at",
    "chat_link",
    "tokens_in",
    "tokens_out",
    "tokens_cache_read",
    "tokens_cache_write",
    "tokens_reasoning",
]


def _expand_row(r):  # type: ignore[no-untyped-def]
    """Fill the optional tail. A 6-field row is the bare base; a 9-field row
    adds archived/hidden/profile; an 8-field row carries the in/out pair on the
    bare base; an 11-field row carries it after the flags; and 14-field rows
    are complete (flags plus all five token counts)."""
    row = tuple(r)
    if len(row) == 6:
        return row + (0, 0, None, 0, 0, 0, 0, 0)
    if len(row) == 8:
        # in/out with no flags/profile: insert the defaults between
        return row[:6] + (0, 0, None) + row[6:] + (0, 0, 0)
    if len(row) == 9:
        return row + (0, 0, 0, 0, 0)
    if len(row) == 11:
        # base + flags + in/out: the cache and reasoning counts default
        return row + (0, 0, 0)
    return row


def _state(home: Path, sessions=(), with_sessions_table: bool = True, pinned_ids=()) -> Path:  # type: ignore[no-untyped-def]
    """Create ``<home>/state.db`` and fill the sessions table.

    A row is ``(id, source, parent_session_id, title, started_at,
    last_activity_at, archived, hidden, profile_name)``, with the trailing flags
    and profile optional (they default to visible / NULL profile); a row may also
    carry ``input_tokens, output_tokens`` at the end (they default to 0).
    ``with_sessions_table=False`` builds a store whose schema has drifted.
    ``pinned_ids`` stamps the store's durable keep flag on those ids after the
    insert — the pin lives in the store, never in this suite's row tuples.
    """
    db = home / "state.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    if with_sessions_table:
        con.executescript(SESSIONS)
        con.executemany(
            "insert into sessions (id, source, parent_session_id, title, started_at,"
            " last_activity_at, archived, hidden, profile_name, input_tokens,"
            " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens)"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [_expand_row(r) for r in sessions],
        )
        if pinned_ids:
            marks = ",".join("?" for _ in pinned_ids)
            con.execute(f"update sessions set pinned = 1 where id in ({marks})", list(pinned_ids))
    else:
        con.executescript("create table something_else (x);")
    con.commit()
    con.close()
    return db


def _home(tmp_path: Path, *sessions) -> Path:  # type: ignore[no-untyped-def]
    _state(tmp_path, sessions)
    return tmp_path


def test_state_db_path_is_under_hermes_home(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert state_db(tmp_path) == tmp_path / "state.db"


def test_connect_state_ro_refuses_writes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """I8 on the new read path: the URI is read-only, so a write raises."""
    db = _state(tmp_path, [("s1", "local", None, "one", 10, 20)])
    con = connect_state_ro(db)
    try:
        with con:
            con.execute("update sessions set title = 'hacked' where id = 's1'")
        raise AssertionError("a read-only connection accepted a write")
    except sqlite3.Error:
        pass
    finally:
        con.close()
    check = sqlite3.connect(db)
    try:
        assert check.execute("select title from sessions").fetchone() == ("one",)
    finally:
        check.close()


def test_root_chats_drops_every_subagent_and_child_session(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A root chat has no parent: subagents do, and so does a spawned child chat.

    The child here carries a *human* source, so the source filter alone would
    keep it — the parent rule is what excludes it.
    """
    home = _home(
        tmp_path,
        ("s_root", "tui", None, "a root chat", 10, 30),
        ("s_sub", "subagent", "s_root", "subagent run", 11, 31),
        ("s_child", "local", "s_root", "spawned child chat", 12, 32),
    )
    assert [r["session_id"] for r in root_chats(home)] == ["s_root"]


def test_root_chats_default_sources_are_the_human_ones(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """kanban/cron/oneshot/subagent sessions are workers or timers, not chats."""
    home = _home(
        tmp_path,
        ("s_local", "local", None, "local chat", 10, 40),
        ("s_tui", "tui", None, "tui chat", 11, 41),
        ("s_kanban", "kanban", None, "worker session", 12, 42),
        ("s_cron", "cron", None, "cron tick", 13, 43),
        ("s_oneshot", "oneshot", None, "oneshot run", 14, 44),
        ("s_subagent", "subagent", None, "child agent", 15, 45),
    )
    assert ROOT_SOURCES == ("local", "tui")
    assert [r["session_id"] for r in root_chats(home)] == ["s_tui", "s_local"]
    assert [r["source"] for r in root_chats(home)] == ["tui", "local"]


def test_root_chats_sources_can_widen_to_cron_and_oneshot(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(
        tmp_path,
        ("s_local", "local", None, "local chat", 10, 40),
        ("s_cron", "cron", None, "cron tick", 11, 41),
        ("s_oneshot", "oneshot", None, "oneshot run", 12, 42),
    )
    widened = root_chats(home, sources=("local", "cron", "oneshot"))
    assert [r["session_id"] for r in widened] == ["s_oneshot", "s_cron", "s_local"]
    # an unknown source simply matches no row rather than erroring
    assert root_chats(home, sources=("carrier-pigeon",)) == []
    # and an explicit empty selection selects nothing
    assert root_chats(home, sources=()) == []


def test_root_chats_drop_archived_and_hidden_sessions(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(
        tmp_path,
        ("s_live", "local", None, "live chat", 10, 50, 0, 0, "default"),
        ("s_arch", "local", None, "archived chat", 11, 51, 1, 0, "default"),
        ("s_auto", "local", None, "auto archived chat", 12, 52, 1, 0, "default"),
        ("s_hidden", "local", None, "hidden chat", 13, 53, 0, 1, "default"),
    )
    assert [r["session_id"] for r in root_chats(home)] == ["s_live"]


def test_root_chats_newest_activity_first_and_capped_at_25(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rows = [("s%02d" % i, "local", None, "chat %02d" % i, float(i), float(i)) for i in range(40)]
    home = _home(tmp_path, *rows)
    got = root_chats(home)
    assert ROOT_CHAT_CAP == 25
    assert len(got) == 25
    assert [r["session_id"] for r in got] == ["s%02d" % i for i in range(39, 14, -1)]
    assert [r["last_activity_at"] for r in got] == [float(i) for i in range(39, 14, -1)]


def test_root_chats_honours_an_explicit_limit(tmp_path) -> None:  # type: ignore[no-untyped-def]
    rows = [("s%02d" % i, "local", None, "chat %02d" % i, float(i), float(i)) for i in range(30)]
    home = _home(tmp_path, *rows)
    assert [r["session_id"] for r in root_chats(home, limit=3)] == ["s29", "s28", "s27"]
    assert root_chats(home, limit=0) == []
    # SQLite reads `LIMIT -1` as "no limit", so a negative cap must be refused
    # rather than handed to the query as the whole store
    assert root_chats(home, limit=-1) == []


def test_root_chats_sort_on_activity_falling_back_to_started_at(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The live store leaves ``last_activity_at`` NULL on most tui rows.

    Measured on this host 2026-11-04: 26 of the 33 root local/tui chats have no
    ``last_activity_at`` at all, so sorting on that column alone would park every
    one of them below the 7 dated rows in an arbitrary id order. The store's own
    ``idx_sessions_effective_activity`` index uses ``coalesce(last_activity_at,
    started_at)``, and so does this read: a fresh session with no recorded
    activity still reads as fresh.
    """
    home = _home(
        tmp_path,
        ("s_fresh_noact", "tui", None, None, 900.0, None),
        ("s_old_active", "local", None, "old but active", 100.0, 500.0),
        ("s_undated_old", "tui", None, None, 50.0, None),
        ("s_mid", "local", None, "mid", 400.0, 450.0),
    )
    assert [r["session_id"] for r in root_chats(home)] == [
        "s_fresh_noact",
        "s_old_active",
        "s_mid",
        "s_undated_old",
    ]
    # the row carries the effective time, so the UI's "last activity" cell is
    # never blank for a session that has a start time
    assert [r["last_activity_at"] for r in root_chats(home)] == [900.0, 500.0, 450.0, 50.0]


def test_root_chats_row_shape_title_fallback_and_link(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(
        tmp_path,
        ("s1", "tui", None, None, 10.0, 20.0),
        ("s2", "local", None, "My chat", 11.0, 21.0, 0, 0, "researcher-a"),
    )
    got = root_chats(home)
    assert [sorted(r) for r in got] == [sorted(ROW_KEYS)] * 2
    named, untitled = got  # newest activity first: s2 (21) before s1 (20)
    assert untitled["session_id"] == "s1"
    assert untitled["title"] == UNTITLED == "(untitled)"
    assert untitled["source"] == "tui"
    # a NULL profile_name reads as the default profile
    assert untitled["profile"] == "default"
    assert untitled["chat_link"] == "/chat?profile=default&resume=s1"
    assert named["title"] == "My chat"
    assert named["profile"] == "researcher-a"
    assert named["chat_link"] == "/chat?profile=researcher-a&resume=s2"


def test_root_chats_links_are_same_origin_and_percent_encoded(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A session id or profile name is untrusted text: no query smuggling."""
    home = _home(
        tmp_path,
        ("s&x?y", "local", None, "weird id", 10.0, 20.0, 0, 0, "prof file"),
    )
    row = root_chats(home)[0]
    assert row["chat_link"] == "/chat?profile=prof%20file&resume=s%26x%3Fy"
    assert row["chat_link"].startswith("/chat?")


def test_root_chats_missing_state_db_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert root_chats(tmp_path) == []
    assert root_chat_snapshot(tmp_path)["chats"] == []


def test_root_chats_survive_a_store_without_the_sessions_table(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Schema drift or a non-database file must not take the tab down."""
    _state(tmp_path, with_sessions_table=False)
    assert root_chats(tmp_path) == []
    (tmp_path / "state.db").write_bytes(b"not a database")
    assert root_chats(tmp_path) == []


def test_root_chats_skip_a_store_row_with_no_session_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """SQLite lets a TEXT PRIMARY KEY column hold NULL; the resume link cannot.

    A row with no id has nothing to resume, and building a link from ``None``
    would raise outside ``sqlite3.Error`` and take the whole section down.
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SESSIONS)
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values (null, 'local', 'orphan', 10, 99)"
    )
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('', 'local', 'blank id', 11, 98)"
    )
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('s_ok', 'local', 'fine', 12, 20)"
    )
    con.commit()
    con.close()
    assert [r["session_id"] for r in root_chats(tmp_path)] == ["s_ok"]


def test_root_chats_survive_a_time_column_holding_a_blob_or_text(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A drifted or foreign store can hold anything in a time column.

    SQLite applies no affinity to a BLOB, so `last_activity_at` really can come
    back as bytes — and bytes in the payload break the response encoder outside
    `sqlite3.Error`, which would take the whole section down for one bad row.
    Text is useless to a relative-time cell, so it goes the same way: the row is
    kept, its effective time reads as unknown.
    """
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SESSIONS)
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('s_blob', 'local', 'blob time', 10, ?)",
        (b"\xff\xfe",),
    )
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('s_text', 'local', 'text time', 11, 'last tuesday')"
    )
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('s_ok', 'local', 'numbered', 12, 20)"
    )
    con.commit()
    con.close()
    # the drifted values sort by SQLite's type order (blob, then text, then
    # number), not as times — the read does not try to interpret them, it keeps
    # the row and reports its effective time as unknown
    assert [r["session_id"] for r in root_chats(tmp_path)] == ["s_blob", "s_text", "s_ok"]
    assert [r["last_activity_at"] for r in root_chats(tmp_path)] == [None, None, 20]


def test_root_chats_blank_title_and_blank_profile_use_the_fallbacks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An empty string is as unusable as a missing value.

    A blank title renders as an empty row; a blank `profile_name` would put
    `?profile=` in the resume link and ask the dashboard to resume in no profile
    at all.
    """
    home = _home(
        tmp_path,
        ("s_blank", "local", None, "", 10.0, 30.0, 0, 0, ""),
        ("s_null", "local", None, None, 11.0, 31.0, 0, 0, None),
        ("s_real", "local", None, "named", 12.0, 32.0, 0, 0, "reviewer"),
    )
    assert [(r["title"], r["profile"], r["chat_link"]) for r in root_chats(home)] == [
        ("named", "reviewer", "/chat?profile=reviewer&resume=s_real"),
        (UNTITLED, "default", "/chat?profile=default&resume=s_null"),
        (UNTITLED, "default", "/chat?profile=default&resume=s_blank"),
    ]


def test_root_chats_break_a_time_tie_on_session_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The cap only means something if a full page of equal times is stable.

    Two chats with the same effective activity must come back in the same order
    every read, or `limit` can drop a different row each refresh.
    """
    home = _home(
        tmp_path,
        ("s_b", "local", None, "b", 10.0, 50.0),
        ("s_a", "local", None, "a", 11.0, 50.0),
        ("s_c", "local", None, "c", 12.0, None),
    )
    assert [r["session_id"] for r in root_chats(home)] == ["s_a", "s_b", "s_c"]
    assert [r["session_id"] for r in root_chats(home, limit=2)] == ["s_a", "s_b"]


def test_the_store_connection_is_closed_after_every_read(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The tab polls this section on an interval, so a leaked connection leaks an
    fd every tick. Both paths close: the one that read rows (chats query and
    token lookup, two connections) and the one whose query failed."""
    home = _home(tmp_path, ("s1", "local", None, "chat", 10.0, 20.0))
    closed: list[str] = []

    class _Tracking(sqlite3.Connection):
        def close(self) -> None:
            closed.append("closed")
            super().close()

    def _spy(db: Path) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, factory=_Tracking)

    monkeypatch.setattr(store, "connect_state_ro", _spy)
    assert [r["session_id"] for r in root_chats(home)] == ["s1"]
    assert closed == ["closed", "closed"]
    (tmp_path / "state.db").write_bytes(b"not a database")
    assert root_chats(home) == []
    assert closed == ["closed", "closed", "closed"]


def test_connect_state_ro_waits_for_the_write_lock(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The gateway holds write locks on this store constantly, so the read waits —
    bounded. Without the timeout a lock collision raises `sqlite3.OperationalError`
    and the error-swallow reports an empty section: a broken watcher reading as a
    clean host."""
    db = _state(tmp_path, [("s1", "local", None, "one", 10.0, 20.0)])
    con = connect_state_ro(db)
    try:
        assert con.execute("pragma busy_timeout").fetchone()[0] == 2000
    finally:
        con.close()


def test_an_empty_source_selection_never_opens_the_store(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Nothing can match, so the store is not touched — the same short-circuit the
    board scan's empty status selection uses. Without it, a dropped source filter
    would still answer "no rows" from a SQL error, and the difference would be
    invisible."""
    _home(tmp_path, ("s1", "local", None, "chat", 10.0, 20.0))
    real_connect = store.connect_state_ro

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("the store was opened for an empty source selection")

    monkeypatch.setattr(store, "connect_state_ro", _boom)
    assert root_chats(tmp_path, sources=()) == []
    # and a non-empty selection does reach it, so the stub is not fooling itself
    monkeypatch.setattr(store, "connect_state_ro", real_connect)
    assert [r["session_id"] for r in root_chats(tmp_path)] == ["s1"]


def test_root_chats_read_hermes_home_env(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No argument means the live HERMES_HOME store — pinned through the env."""
    _home(tmp_path, ("s1", "local", None, "chat", 10.0, 20.0))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert [r["session_id"] for r in root_chats()] == ["s1"]


def test_root_chats_select_ignores_the_columns_this_read_does_not_name(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The live table has ~60 columns; the read names only the slice it needs."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.execute(
        "create table sessions (id text primary key, source text not null,"
        " parent_session_id text, title text, started_at real, last_activity_at real,"
        " archived integer not null default 0, hidden integer not null default 0,"
        " profile_name text, model text, cwd text, transcript_revision integer,"
        " pinned integer not null default 0)"
    )
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at,"
        " model, pinned) values ('s1','local','pinned chat',10,20,'m',1)"
    )
    con.commit()
    con.close()
    assert [r["title"] for r in root_chats(tmp_path)] == ["pinned chat"]


def test_root_chat_snapshot_echoes_the_sources_it_selected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(
        tmp_path,
        ("s_local", "local", None, "local chat", 10.0, 20.0),
        ("s_cron", "cron", None, "cron tick", 11.0, 21.0),
    )
    default = root_chat_snapshot(home)
    assert sorted(default) == ["chats", "filters"]
    assert default["filters"] == {"sources": list(ROOT_SOURCES), "limit": ROOT_CHAT_CAP}
    assert [r["session_id"] for r in default["chats"]] == ["s_local"]

    widened = root_chat_snapshot(home, sources=("local", "cron"), limit=1)
    assert widened["filters"] == {"sources": ["local", "cron"], "limit": 1}
    assert [r["session_id"] for r in widened["chats"]] == ["s_cron"]


# --- pins (ADR-0015) ----------------------------------------------------------


def test_pinned_chat_sorts_before_a_newer_unpinned_one(tmp_path) -> None:
    """The store's durable keep flag is the ordering contract: a pinned chat
    heads the section however much newer the unpinned traffic below it gets."""
    _state(
        tmp_path,
        [
            ("s_old", "tui", None, "pinned master", 10.0, 100.0),
            ("s_new", "local", None, "busy day", 11.0, 900.0),
        ],
        pinned_ids=("s_old",),
    )
    got = root_chats(tmp_path)
    assert [r["session_id"] for r in got] == ["s_old", "s_new"]
    assert [r["pinned"] for r in got] == [True, False]


def test_unpinned_chats_keep_their_recency_order_under_a_pinned_one(tmp_path) -> None:
    """The pin reorders pinned vs unpinned only: the unpinned tail is still
    newest activity first among itself."""
    _state(
        tmp_path,
        [
            ("s_pin", "tui", None, "pinned", 1.0, 10.0),
            ("s_a", "local", None, "a", 2.0, 300.0),
            ("s_b", "local", None, "b", 3.0, 200.0),
        ],
        pinned_ids=("s_pin",),
    )
    assert [r["session_id"] for r in root_chats(tmp_path)] == ["s_pin", "s_a", "s_b"]


def test_pinned_chats_past_the_cap_are_backfilled(tmp_path) -> None:
    """A pinned chat never drops off the section by age alone: pinned rows
    outside the recency page join it after the page, the way the store's own
    listings back-fill pinned rows past their limit."""
    _state(
        tmp_path,
        [
            ("p_a", "tui", None, "pinned newest", 1.0, 700.0),
            ("p_b", "tui", None, "pinned mid", 2.0, 600.0),
            ("p_c", "tui", None, "pinned oldest", 3.0, 500.0),
            ("u_1", "local", None, "busy", 4.0, 900.0),
            ("u_2", "local", None, "busier", 5.0, 800.0),
        ],
        pinned_ids=("p_a", "p_b", "p_c"),
    )
    got = root_chats(tmp_path, limit=2)
    # the page takes pinned rows first, then back-fills the pinned row the
    # cap left out; unpinned rows compete only for what the cap still holds
    assert [(r["session_id"], r["pinned"]) for r in got] == [
        ("p_a", True),
        ("p_b", True),
        ("p_c", True),
    ]


def test_the_pinned_backfill_is_capped_like_the_page(tmp_path) -> None:
    """The back-fill answers at most ``limit`` rows of its own, so a store with
    a hundred pins cannot make one section unbounded."""
    rows = [(f"p_{i}", "tui", None, f"pin {i}", float(i), float(i)) for i in range(1, 6)]
    _state(tmp_path, rows, pinned_ids=[r[0] for r in rows])
    assert [r["session_id"] for r in root_chats(tmp_path, limit=2)] == ["p_5", "p_4", "p_3", "p_2"]


def test_backfill_skips_rows_the_page_already_kept(tmp_path) -> None:
    """A pinned row inside the page appears exactly once — the back-fill is
    for pinned rows outside the page, not a duplicate listing."""
    _state(
        tmp_path,
        [("p_a", "tui", None, "pinned", 1.0, 700.0), ("u_1", "local", None, "u", 2.0, 900.0)],
        pinned_ids=("p_a",),
    )
    assert [r["session_id"] for r in root_chats(tmp_path, limit=1)] == ["p_a"]


def test_store_without_a_pinned_column_still_answers(tmp_path) -> None:
    """A drifted or older store with no ``pinned`` column keeps its chats:
    every row reads unpinned in the old recency order — no pins, like no
    tokens, beats no section."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(SESSIONS_OLD)
    con.executemany(
        "insert into sessions (id, source, title, started_at, last_activity_at) values (?,?,?,?,?)",
        [("s_a", "local", "a", 1.0, 100.0), ("s_b", "local", "b", 2.0, 200.0)],
    )
    con.commit()
    con.close()
    got = root_chats(tmp_path)
    assert [r["session_id"] for r in got] == ["s_b", "s_a"]
    assert [r["pinned"] for r in got] == [False, False]
    assert [sorted(r) for r in got] == [sorted(ROW_KEYS)] * 2


def test_pinned_flag_coerces_a_drifted_column_to_false(tmp_path) -> None:
    """Same discipline as the time and token columns: a blob or text in
    ``pinned`` reads as unpinned instead of reaching the payload raw."""
    home = _home(tmp_path, ("s1", "tui", None, "blob pin", 10.0, 20.0))
    con = sqlite3.connect(home / "state.db")
    con.execute("update sessions set pinned = x'00' where id = 's1'")
    con.commit()
    con.close()
    assert [r["pinned"] for r in root_chats(home)] == [False]


# --- token usage (ADR-0010) ---------------------------------------------------
#
# The store counts tokens per session (`input_tokens` / `output_tokens`), and
# every Worker Watch row that carries a session id can ask for them. The lookup
# must fail closed — a missing store, a drifted schema or a blob in a token
# column answers with no tokens, never with a row the encoder cannot encode and
# never with the section lost.


def test_root_chat_rows_carry_token_counts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(
        tmp_path,
        (
            "s_local",
            "local",
            None,
            "local chat",
            10.0,
            20.0,
            0,
            0,
            "default",
            1200,
            640,
            900,
            5,
            40,
        ),
    )
    row = root_chats(home)[0]
    assert (row["tokens_in"], row["tokens_out"]) == (1200, 640)
    # the cache and reasoning counts ride along: a worker that read a big
    # cached prompt every turn looks very different from one that paid for it
    assert (row["tokens_cache_read"], row["tokens_cache_write"]) == (900, 5)
    assert row["tokens_reasoning"] == 40


def test_token_usage_resolves_only_known_ids(tmp_path) -> None:  # type: ignore[no-untyped-def]
    home = _home(
        tmp_path,
        ("s_a", "kanban", None, "worker", 1.0, 2.0, 0, 0, "default", 300, 40, 1200, 30, 55),
        ("s_b", "local", None, "chat", 1.0, 2.0, 0, 7),
    )
    assert token_usage(["s_a", "s_b", "s_missing"], home) == {
        "s_a": {"in": 300, "out": 40, "cache_read": 1200, "cache_write": 30, "reasoning": 55},
        "s_b": {"in": 0, "out": 7, "cache_read": 0, "cache_write": 0, "reasoning": 0},
    }


def test_token_usage_never_opens_the_store_for_an_empty_id_list(tmp_path) -> None:  # type: ignore[no-untyped-def]
    missing = tmp_path / "nothing-here"
    assert token_usage([], home=missing) == {}
    assert token_usage([None, "", 5], home=missing) == {}


def test_token_usage_of_a_missing_or_drifted_store_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert token_usage(["s1"], home=tmp_path) == {}
    _state(tmp_path, with_sessions_table=False)
    assert token_usage(["s1"], home=tmp_path) == {}


def test_token_usage_of_an_unusable_id_list_is_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The short-circuit is defensive down to the argument itself: `None` (or
    any non-iterable) never reaches the store and never raises."""
    missing = tmp_path / "nothing-here"
    assert token_usage(None, home=missing) == {}
    assert token_usage(7, home=missing) == {}


def test_token_usage_coerces_drifted_token_columns_to_none(tmp_path) -> None:  # type: ignore[no-untyped-def]
    db = _state(tmp_path, [("s1", "local", None, "one", 10.0, 20.0)])
    con = sqlite3.connect(db)
    con.execute(
        "update sessions set input_tokens = x'dead', output_tokens = 'text',"
        " cache_read_tokens = 1.5, cache_write_tokens = x'00', reasoning_tokens = 'x'"
    )
    con.commit()
    con.close()
    assert token_usage(["s1"], home=tmp_path) == {
        "s1": {"in": None, "out": None, "cache_read": None, "cache_write": None, "reasoning": None}
    }


def test_root_chat_rows_survive_a_store_without_token_columns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """An older store slice keeps its chats even with no token columns at all."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(
        "create table sessions (id text primary key, source text not null,"
        " parent_session_id text, title text, started_at real, last_activity_at real,"
        " archived integer not null default 0, hidden integer not null default 0,"
        " profile_name text);"
    )
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('s1','local','old store chat',10,20)"
    )
    con.commit()
    con.close()
    rows = root_chats(tmp_path)
    assert [r["title"] for r in rows] == ["old store chat"]
    assert (rows[0]["tokens_in"], rows[0]["tokens_out"]) == (None, None)
    assert (rows[0]["tokens_cache_read"], rows[0]["tokens_cache_write"]) == (None, None)
    assert rows[0]["tokens_reasoning"] is None
