"""Print the dashboard chat-session URL for a kanban task's worker session.

Reads the board's task_events read-only (the same feed the dashboard drawer
shows) and resolves the newest `owner_admitted` payload's session_id into
/chat?profile=<profile>&resume=<session_id>. `chat_path()` (the relative link)
and `latest_admitted()` / `latest_admitted_session_id()` (the payload scan over
an open connection, with and without the event time) are the shared pieces the
Worker Watch dashboard plugin reuses server-side.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def board_db(board: str) -> Path:
    return hermes_home() / "kanban" / "boards" / board / "kanban.db"


def current_board() -> str:
    pointer = hermes_home() / "kanban" / "current"
    try:
        return pointer.read_text().strip() or "default"
    except OSError:
        return "default"


def chat_path(profile: str, session_id: str) -> str:
    """Same-origin relative chat-resume link (what in-dashboard UIs navigate to).

    Values are percent-encoded so a hostile payload cannot smuggle extra
    query params into the link.
    """
    p = quote(profile, safe="")
    sid = quote(session_id, safe="")
    return f"/chat?profile={p}&resume={sid}"


def latest_admitted(con: sqlite3.Connection, task_id: str) -> tuple[str, int | None] | None:
    """``(session_id, event created_at)`` for the newest `owner_admitted` payload with one.

    Window: the 5 newest rows (I9). Takes an open connection; the query is a
    SELECT, so read-only connections are fine. The event time is what the
    Worker Watch history section sorts on.
    """
    rows = con.execute(
        "select payload, created_at from task_events where task_id=? and kind='owner_admitted'"
        " order by id desc limit 5",
        (task_id,),
    ).fetchall()
    for payload, created_at in rows:
        try:
            parsed = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("session_id"):
            return str(parsed["session_id"]), created_at
    return None


def latest_admitted_session_id(con: sqlite3.Connection, task_id: str) -> str | None:
    """Session id of the newest `owner_admitted` payload that parses and carries one."""
    found = latest_admitted(con, task_id)
    return found[0] if found else None


def session_url(
    db: Path, task_id: str, *, profile: str = "default", host: str = "127.0.0.1", port: int = 9119
) -> str | None:
    if not db.exists():
        return None
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sid = latest_admitted_session_id(con, task_id)
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return f"http://{host}:{port}{chat_path(profile, sid)}" if sid else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("task_id")
    ap.add_argument("--board", default=None, help="board slug (default: current board)")
    ap.add_argument("--profile", default="default")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9119)
    ns = ap.parse_args(argv)
    board = ns.board or current_board()
    url = session_url(board_db(board), ns.task_id, profile=ns.profile, host=ns.host, port=ns.port)
    if url is None:
        print(f"no worker session recorded for {board}/{ns.task_id} yet", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
