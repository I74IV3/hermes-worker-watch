"""Worker Watch — running cards, recent worker sessions and the per-board
relationship graph across every kanban board.

The read side of the Worker Watch dashboard plugin (ADR-0002, extended by
ADR-0004). Enumerate ``<hermes_home>/kanban/boards/<slug>/kanban.db`` (board
dirs starting with ``_`` are skipped: ``_archived``), and build one row shape
for two sections:

* ``running_workers()`` — tasks whose status column is ``running``, sorted by
  ``(board, task_id)``.
* ``recent_sessions()`` — tasks whose status is ``done``, ``review`` or
  ``blocked`` (``HISTORY_STATUSES``), newest worker session first, capped at
  ``HISTORY_CAP``. A card with no usable ``owner_admitted`` session has nothing
  to resume, so it is left out of the section entirely.

Each row carries its newest ``owner_admitted`` session as a same-origin
``/chat`` link via ``workerlink.chat_path``, plus that event's ``created_at``
(``session_at``) — the recency key for the history section. Board DBs open only
through the ``file:…?mode=ro`` URI (I8); a corrupt or schema-drifted board is
skipped so one bad file never blanks the table.

The tree layer — ``task_links`` into ``parent_id``/``depth``, tree order and the
board grouping — lives in ``workerwatch/task_tree.py`` and is imported here
(ADR-0005); this module stays the DB read side. ``board_graph`` and
``graph_snapshot`` use the same edges the other way round: every card a node,
every link an edge, for the relationship SVG (ADR-0006).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .root_chats import attach_tokens
from .task_tree import board_groups, depths, layer_depths, link_edges, parents_of
from .workerlink import chat_path, hermes_home, latest_admitted

RUNNING = ("running",)
HISTORY_STATUSES = ("done", "review", "blocked")
WATCH_STATUSES = RUNNING + HISTORY_STATUSES
HISTORY_CAP = 25


def board_dbs(home: Path) -> list[tuple[str, Path]]:
    """``(slug, db)`` for every board dir holding a kanban.db, sorted by slug.

    Names beginning with ``_`` (e.g. ``_archived``) and dirs without a DB are
    skipped, as are stray files.
    """
    boards = home / "kanban" / "boards"
    try:
        entries = sorted(boards.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    found: list[tuple[str, Path]] = []
    for entry in entries:
        if entry.name.startswith("_") or not entry.is_dir():
            continue
        db = entry / "kanban.db"
        if db.is_file():
            found.append((entry.name, db))
    return found


def connect_ro(db: Path) -> sqlite3.Connection:
    """The one connection shape allowed against a board DB: read-only URI."""
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _run_of(con: sqlite3.Connection, task_id: str, run_id: int | None) -> tuple[Any, Any]:
    """``(profile, started_at)`` of the card's current run, else its newest run."""
    if run_id is not None:
        row = con.execute("select profile, started_at from task_runs where id=?", (run_id,))
    else:
        row = con.execute(
            "select profile, started_at from task_runs where task_id=?"
            " order by started_at desc, id desc limit 1",
            (task_id,),
        )
    found = row.fetchone()
    return found if found else (None, None)


def _rows_for_board(
    con: sqlite3.Connection,
    slug: str,
    statuses: tuple[str, ...],
    assignee: str | None = None,
) -> list[dict[str, Any]]:
    """One row per task on this board whose status column is in ``statuses``.

    ``assignee`` narrows the scan to one assignee (the `assignee` column, read
    as a column — an unassigned card only shows in an unfiltered scan).
    Each row also carries its place in the board's parent-child tree
    (``parent_id`` + ``depth``) read from ``task_links``, so the UI can indent
    children under their parents inside a board group.
    """
    parents = parents_of(con)
    card_depths = depths(parents)
    marks = ",".join("?" for _ in statuses)
    args: list[Any] = list(statuses)
    where = f"status in ({marks})"
    if assignee is not None:
        where += " and assignee = ?"
        args.append(assignee)
    tasks = con.execute(
        f"select id, title, assignee, status, current_run_id, started_at from tasks where {where}",
        args,
    ).fetchall()
    rows: list[dict[str, Any]] = []
    for task_id, title, task_assignee, status, run_id, task_started in tasks:
        profile, run_started = _run_of(con, task_id, run_id)
        profile = profile or "default"
        run_started = run_started if run_started is not None else task_started
        admitted = latest_admitted(con, task_id)
        sid, session_at = admitted if admitted else (None, None)
        # a schema-drifted board can store created_at as text; a row with no usable
        # time still lists (its link works), it just sorts last and renders "—"
        session_at = session_at if isinstance(session_at, int) else None
        rows.append(
            {
                "board": slug,
                "task_id": task_id,
                "parent_id": parents.get(task_id),
                "depth": card_depths.get(task_id, 0),
                "title": title,
                "assignee": task_assignee,
                "status": status,
                "profile": profile,
                "run_started_at": run_started,
                "session_id": sid,
                "session_at": session_at,
                "chat_link": chat_path(profile, sid) if sid else None,
            }
        )
    return rows


def _scan(
    home: Path,
    statuses: tuple[str, ...],
    *,
    board: str | None = None,
    assignee: str | None = None,
) -> list[dict[str, Any]]:
    """Rows for ``statuses`` on every readable board; a bad board is skipped.

    ``board`` limits the scan to one board slug — an unknown slug simply yields
    no rows, since it is a slug compare, not a lookup that can fail — and
    ``assignee`` limits it to one assignee.
    """
    rows: list[dict[str, Any]] = []
    if not statuses:
        return rows
    for slug, db in board_dbs(home):
        if board is not None and slug != board:
            continue
        try:
            con = connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            rows.extend(_rows_for_board(con, slug, statuses, assignee))
        except sqlite3.Error:
            continue
        finally:
            con.close()
    return rows


def running_workers(
    home: Path | None = None,
    *,
    board: str | None = None,
    assignee: str | None = None,
) -> list[dict[str, Any]]:
    """Every running card on every board, sorted by ``(board, task_id)``.

    ``home`` defaults to ``HERMES_HOME`` (or ``~/.hermes``). A board whose DB
    cannot be opened or queried is skipped — a broken watcher must never blank
    the other boards' rows. ``board`` and ``assignee`` are the tab's filters.
    """
    rows = _scan(home or hermes_home(), RUNNING, board=board, assignee=assignee)
    rows.sort(key=lambda r: (r["board"], r["task_id"]))
    return rows


def recent_sessions(
    home: Path | None = None,
    *,
    board: str | None = None,
    assignee: str | None = None,
    statuses: tuple[str, ...] = HISTORY_STATUSES,
    limit: int = HISTORY_CAP,
) -> list[dict[str, Any]]:
    """The newest finished/held cards that still have a resumable session.

    Cards in ``statuses`` (by default ``HISTORY_STATUSES``: done, review,
    blocked), ordered by the time of their newest ``owner_admitted`` session
    (newest first; ties break on board then task id) and capped at ``limit``. An
    empty ``statuses`` selects nothing — the API's status checkboxes rely on
    that. A card whose session scan finds nothing is skipped: the section exists
    to offer a resume link, and without a session there is nothing to resume.
    """
    rows = [
        r
        for r in _scan(home or hermes_home(), statuses, board=board, assignee=assignee)
        if r["session_id"]
    ]
    rows.sort(key=lambda r: (-(r["session_at"] or 0), r["board"], r["task_id"]))
    return rows[: max(0, limit)]


def board_graph(con: sqlite3.Connection, slug: str) -> dict[str, Any]:
    """One board's relationship graph: every task is a node, every ``task_links``
    edge with both ends on the board is an edge.

    Every status is in the graph, archived included: the graph is the board's
    structure, and a hidden card hides its edges along with it. A card in no
    link at all still gets a node — layer 0, in no edge — which is how the UI
    lists it as unlinked instead of losing it. ``layer_depths`` places a card
    with two parents under its nearest parent, and an edge pointing at a card
    that is no longer on the board is dropped rather than drawn to nowhere.
    """
    tasks = {
        row[0]: row
        for row in con.execute("select id, title, status, current_run_id, started_at from tasks")
    }
    edges = [(p, c) for p, c in link_edges(con) if p in tasks and c in tasks]
    layers = layer_depths(tasks, edges)
    nodes: list[dict[str, Any]] = []
    for task_id in sorted(tasks, key=lambda tid: (layers.get(tid, 0), tid)):
        _row_id, title, status, run_id, _started = tasks[task_id]
        profile = _run_of(con, task_id, run_id)[0] or "default"
        admitted = latest_admitted(con, task_id)
        sid = admitted[0] if admitted else None
        nodes.append(
            {
                "id": task_id,
                "title": title,
                "status": status,
                "depth": layers.get(task_id, 0),
                "session_id": sid,
                "profile": profile,
                "chat_link": chat_path(profile, sid) if sid else None,
            }
        )
    return {
        "board": slug,
        "nodes": nodes,
        "edges": [{"parent": parent, "child": child} for parent, child in edges],
    }


def graph_snapshot(home: Path | None = None, *, board: str | None = None) -> dict[str, Any]:
    """The relationship graph of every board, in one payload.

    ``board`` narrows the payload to that slug and the entry shape does not
    change, so the UI has one renderer either way. A board whose DB cannot be
    opened, or whose schema has no ``tasks`` table, is skipped like anywhere
    else in the read side, and a board with no cards gets no entry rather than an
    empty picture.
    """
    root = home or hermes_home()
    boards: list[dict[str, Any]] = []
    for slug, db in board_dbs(root):
        if board is not None and slug != board:
            continue
        try:
            con = connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            graph = board_graph(con, slug)
        except sqlite3.Error:
            continue
        finally:
            con.close()
        if graph["nodes"]:
            boards.append(graph)
    attach_tokens([n for entry in boards for n in entry["nodes"]], root)
    return {"filters": {"board": board}, "boards": boards}


def _filter_options(home: Path) -> dict[str, list[str]]:
    """Every board slug and every assignee on the host, read before any filter.

    The filter bar needs the full list even while a filter hides the rest — a
    select that only offers the current choice cannot be changed back. A board
    that cannot be opened is absent, because it has no rows to show anyway.
    """
    boards: list[str] = []
    assignees: set[str] = set()
    for slug, db in board_dbs(home):
        try:
            con = connect_ro(db)
        except sqlite3.Error:
            continue
        try:
            found = {row[0] for row in con.execute("select distinct assignee from tasks") if row[0]}
        except sqlite3.Error:
            continue
        finally:
            con.close()
        boards.append(slug)
        assignees |= found
    return {"boards": boards, "assignees": sorted(assignees)}


def watch_snapshot(
    home: Path | None = None,
    *,
    board: str | None = None,
    assignee: str | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int = HISTORY_CAP,
) -> dict[str, Any]:
    """The whole tab in one dict: the filters echoed back, the options the filter
    bar needs, then each section grouped by board with its cards in tree order.

    ``statuses=None`` means no status filter (every watched status); an empty
    tuple means the user unchecked every box, which selects nothing. The history
    cap applies to the flat newest-first selection *before* grouping, so the
    board filter changes which cards the cap keeps — asking for one board gets
    that board's newest ``limit`` cards.
    """
    root = home or hermes_home()
    wanted = WATCH_STATUSES if statuses is None else statuses
    options = _filter_options(root)
    running = running_workers(root, board=board, assignee=assignee) if RUNNING[0] in wanted else []
    recent = recent_sessions(
        root,
        board=board,
        assignee=assignee,
        statuses=tuple(s for s in wanted if s in HISTORY_STATUSES),
        limit=limit,
    )
    groups = board_groups(running, recent)
    attach_tokens([e for g in groups for e in g["running"] + g["recent"]], root)
    return {
        "filters": {"board": board, "assignee": assignee, "statuses": list(wanted)},
        "board_options": options["boards"],
        "assignee_options": options["assignees"],
        "boards": groups,
    }
