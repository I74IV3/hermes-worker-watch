"""One card, fully: the detail read behind the Worker Watch side drawer (ADR-0012).

The tab's tables and graphs answer "which cards exist and how do they relate";
the drawer answers "what was this card doing". Three reads answer it, all
read-only: the card row (facts, body, result, block reason) from the board DB,
its newest events and comments from the same DB, and — when the card admitted
a worker session — that session's meta and token counts through
``root_chats`` (the only module that opens the session store, I8).

Everything here narrows instead of raising: an unknown board or task is no card
at all, an old board slice without body/comments answers with those fields
missing rather than a failed request, a drifted cell reads as ``None`` so the
payload always reaches the response encoder, and a missing session store just
means the drawer has no session half. Caps keep one card's payload bounded no
matter how chatty its comments got."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .root_chats import hermes_home, session_meta, token_usage
from .worker_watch import board_dbs, connect_ro
from .workerlink import latest_admitted

# One card's payload stays bounded even when a worker left a novel in its body:
# the drawer shows the head and says the rest was cut.
BODY_CAP = 4000
RESULT_CAP = 1000
FAILURE_CAP = 400
COMMENT_CAP = 400
EVENT_DETAIL_CAP = 200
EVENT_LIMIT = 8
COMMENT_LIMIT = 3

# The old-schema boards keep resolving: this select is what a minimal board
# slice still answers.
_TASKS_MIN = "select id, title, assignee, status from tasks where id = ?"
_TASKS_FULL = (
    "select id, title, body, assignee, status, block_kind, last_failure_error,"
    " result, created_at, started_at, completed_at, branch_name, workspace_path"
    " from tasks where id = ?"
)


def _text(value: Any, cap: int | None = None) -> str | None:
    """A text cell, or ``None`` — a blob or number in a text column reads as
    missing rather than shipping bytes to the response encoder."""
    if not isinstance(value, str) or not value:
        return None
    return value[:cap] if cap is not None else value


def _num(value: Any) -> int | float | None:
    """A time or count cell as a JSON-safe number, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _board_db(home: Path, slug: str) -> Path | None:
    for name, db in board_dbs(home):
        if name == slug:
            return db
    return None


def _task_row(con: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    try:
        row = con.execute(_TASKS_FULL, (task_id,)).fetchone()
    except sqlite3.Error:
        # an older board slice: the narrow select still answers, and the
        # drawer simply has fewer facts to show
        try:
            row = con.execute(_TASKS_MIN, (task_id,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        rid, title, assignee, status = row
        return {"id": rid, "title": title, "assignee": assignee, "status": status}
    if row is None:
        return None
    (
        rid,
        title,
        body,
        assignee,
        status,
        block_kind,
        failure,
        result,
        created_at,
        started_at,
        completed_at,
        branch,
        workspace,
    ) = row
    return {
        "id": rid,
        "title": title,
        "body": body,
        "assignee": assignee,
        "status": status,
        "block_kind": block_kind,
        "last_failure_error": failure,
        "result": result,
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "branch_name": branch,
        "workspace_path": workspace,
    }


def _event_detail(payload: Any) -> str | None:
    """The human line an event's JSON payload carries, if any: what the worker
    summarized, or who commented. Anything unparsable keeps just kind and time."""
    if not isinstance(payload, str):
        return None
    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("summary", "result_preview", "author"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value[:EVENT_DETAIL_CAP]
    return None


def _events(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    try:
        rows = con.execute(
            "select kind, payload, created_at from task_events where task_id = ?"
            " order by created_at desc, id desc limit ?",
            (task_id, EVENT_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {
            "kind": kind if isinstance(kind, str) else "?",
            "detail": _event_detail(payload),
            "created_at": _num(created_at),
        }
        for kind, payload, created_at in rows
    ]


def _comments(con: sqlite3.Connection, task_id: str) -> list[dict[str, Any]]:
    try:
        rows = con.execute(
            "select author, body, created_at from task_comments where task_id = ?"
            " order by created_at desc, id desc limit ?",
            (task_id, COMMENT_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [
        {"author": _text(author), "body": _text(body, COMMENT_CAP), "created_at": _num(created_at)}
        for author, body, created_at in rows
    ]


def _session(session_id: Any, home: Path) -> dict[str, Any] | None:
    meta = session_meta(session_id, home)
    if meta is None:
        return None
    usage = token_usage([session_id], home).get(str(session_id), {})
    meta["tokens_in"] = usage.get("in")
    meta["tokens_out"] = usage.get("out")
    meta["tokens_cache_read"] = usage.get("cache_read")
    meta["tokens_cache_write"] = usage.get("cache_write")
    meta["tokens_reasoning"] = usage.get("reasoning")
    return meta


def card_detail(board: Any, task_id: Any, home: Path | None = None) -> dict[str, Any] | None:
    """One card from one board, or ``None``: facts and body, newest events and
    comments, and the admitted session's meta with its token counts.

    The board DB opens read-only (I8), and the session half goes through
    ``root_chats`` alone. A chatty body is capped with ``body_truncated`` set,
    so the drawer shows what the card was about without shipping an essay.
    """
    if not isinstance(board, str) or not board or not isinstance(task_id, str) or not task_id:
        return None
    root = home or hermes_home()
    db = _board_db(root, board)
    if db is None:
        return None
    try:
        con = connect_ro(db)
    except sqlite3.Error:
        return None
    try:
        row = _task_row(con, task_id)
        if row is None:
            return None
        # the same join the resume links use (workerlink.latest_admitted over
        # task_events), never the tasks.session_id column — a drawer that
        # named a session the row has no link to would break I17. An old
        # board slice with no task_events table simply has no admission.
        try:
            admitted = latest_admitted(con, task_id)
        except sqlite3.Error:
            admitted = None
        sid = admitted[0] if admitted else None
        raw_body = row.get("body")
        body = _text(raw_body, BODY_CAP)
        truncated = isinstance(raw_body, str) and len(raw_body) > BODY_CAP
        return {
            "board": board,
            "id": row["id"] if isinstance(row["id"], str) else task_id,
            "title": _text(row.get("title")) or "",
            "body": body,
            "body_truncated": truncated,
            "status": _text(row.get("status")),
            "assignee": _text(row.get("assignee")),
            "block_kind": _text(row.get("block_kind")),
            "last_failure_error": _text(row.get("last_failure_error"), FAILURE_CAP),
            "result": _text(row.get("result"), RESULT_CAP),
            "created_at": _num(row.get("created_at")),
            "started_at": _num(row.get("started_at")),
            "completed_at": _num(row.get("completed_at")),
            "branch_name": _text(row.get("branch_name")),
            "workspace_path": _text(row.get("workspace_path")),
            "session_id": sid,
            "events": _events(con, task_id),
            "comments": _comments(con, task_id),
            "session": _session(sid, root),
        }
    finally:
        con.close()
