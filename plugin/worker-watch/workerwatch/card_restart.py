"""Parked cards (read) and the one sanctioned restart (write) for Worker Watch.

A card is **parked** when it is ``ready``, has an assignee, and still carries a
``last_failure_error`` from a requeued run. ``check_respawn_guard`` reads that
text as a quota/auth blocker once a newer non-success run supersedes the
rate-limited one, so the dispatcher never spawns the card again: five exit-75
runs during a provider outage, one reclaimed run, then 5.9 h stranded
(measured 2026-11-05). The Worker Watch tab could not show such a card at all —
its statuses are running/done/review/blocked — which is why the stall was
invisible; ``stalled_snapshot`` is that missing read.

The write side follows the repo rule that a store another process owns is never
written directly: the restart is Hermes' own verb, ``hermes kanban retry``
(overlay patch 0002), which sets ``last_failure_error`` and
``consecutive_failures`` back to NULL for one parked card with an audit event,
after which the gateway spawns it on its next 60 s tick. ``unblock`` clears the
same columns but only for blocked/scheduled cards, and reaching it through a
block/unblock pair trips the block-loop breaker on the second use (measured
2026-11-05: the card landed in ``triage`` with the stamp intact), which is why
this module does not use it. The CLI answering 0 is not evidence — the row is
re-read and a surviving stamp is a failure, so a version skew can never read as
a successful restart.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .worker_watch import board_dbs, connect_ro
from .workerlink import hermes_home

Runner = Callable[..., Any]

# Board slugs and task ids are path segments in a request; anything with a
# separator, a parent hop or a blank is a typo at best and a traversal at worst.
SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Only a quota/rate-limit stamp is the failure the guard misreads. A crash or a
# traceback is a real diagnosis and must keep parking the card.
QUOTA_FLAVOURED = re.compile(r"rate[-\s_]?limit|exited rate|quota|\b429\b", re.IGNORECASE)

HISTORY_COLUMNS = "id, title, assignee, status, last_failure_error"


def _stalled_rows(con: sqlite3.Connection, slug: str) -> list[dict[str, Any]]:
    rows = con.execute(
        "select " + HISTORY_COLUMNS + " from tasks"
        " where status = 'ready'"
        " and assignee is not null and trim(assignee) <> ''"
        " and last_failure_error is not null and trim(last_failure_error) <> ''"
        " order by id"
    ).fetchall()
    return [
        {
            "board": slug,
            "id": r[0],
            "title": r[1],
            "assignee": r[2],
            "status": r[3],
            "error": r[4],
        }
        for r in rows
    ]


def stalled_snapshot(home: Path | None = None, *, board: str | None = None) -> dict[str, Any]:
    """Every board's parked cards, one entry per board that has any.

    Boards open read-only and an unopenable one is skipped, exactly like the
    rest of the read side; a board with no parked card gets no entry rather than
    an empty table.
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
            rows = _stalled_rows(con, slug)
        except sqlite3.Error:
            continue
        finally:
            con.close()
        if rows:
            boards.append({"board": slug, "rows": rows})
    return {"boards": boards, "count": sum(len(b["rows"]) for b in boards)}


def _no(status: int, reason: str) -> dict[str, Any]:
    return {"ok": False, "status": status, "reason": reason}


def _row(db: Path, board: str, task_id: str) -> tuple[Any, ...] | None:
    con = connect_ro(db)
    try:
        return con.execute(
            "select " + HISTORY_COLUMNS + ", claim_lock, current_run_id from tasks where id = ?",
            (task_id,),
        ).fetchone()
    finally:
        con.close()


def _refusal(row: tuple[Any, ...]) -> str | None:
    """Why this row cannot be restarted, or None when it is the parked shape."""
    _id, _title, assignee, status, stamp, claim_lock, current_run_id = row
    if status != "ready":
        return f"card is {status}, not ready"
    if not (assignee or "").strip():
        return "unassigned: assign a profile first"
    if claim_lock or current_run_id is not None:
        return "a run is active on this card: reclaim it first"
    if not (stamp or "").strip():
        return "no failure stamp to clear"
    if not QUOTA_FLAVOURED.search(stamp):
        return f"failure text is not a quota/rate-limit stamp: {stamp[:120]}"
    return None


def _run_step(runner: Runner, board: str, verb: str, task_id: str) -> str | None:
    """One CLI leg; returns the failure text, or None when it answered 0."""
    argv = [os.environ.get("HERMES_BIN", "hermes"), "kanban", "--board", board, verb, task_id]
    done = runner(argv, capture_output=True, text=True)
    if done.returncode == 0:
        return None
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return f"{verb} failed (rc={done.returncode}): {detail[-1] if detail else 'no output'}"


def restart_card(
    board: str,
    task_id: str,
    *,
    home: Path | None = None,
    runner: Runner | None = None,
) -> dict[str, Any]:
    """Clear a parked card's stale stamp through the CLI and confirm it.

    Refusals never shell out. One leg, ``retry`` (the verb that resets the stamp
    without a block/unblock cycle); a non-zero answer names itself, and a stamp
    that survives a zero exit is reported as such.
    """
    if not SLUG.match(board or "") or not SLUG.match(task_id or ""):
        return _no(400, "board and task must be a slug and a task id")
    root = home or hermes_home()
    db = root / "kanban" / "boards" / board / "kanban.db"
    if not db.is_file():
        return _no(404, f"no board {board!r}")
    try:
        row = _row(db, board, task_id)
    except sqlite3.Error as exc:
        return _no(500, f"board {board!r} could not be read: {exc}")
    if row is None:
        return _no(404, f"no card {task_id!r} on board {board!r}")
    refusal = _refusal(row)
    if refusal is not None:
        return _no(409, refusal)

    stamp = row[4]
    run = runner if runner is not None else _default_runner()
    failed = _run_step(run, board, "retry", task_id)
    if failed is not None:
        return _no(502, failed)
    steps = ["retry"]

    after = _row(db, board, task_id)
    if after is None:
        return _no(502, f"card {task_id!r} vanished mid-restart")
    if (after[4] or "").strip():
        return _no(502, f"the failure stamp survived the CLI ({after[4][:120]})")
    return {
        "ok": True,
        "board": board,
        "task_id": task_id,
        "status": after[3],
        "cleared_error": stamp,
        "steps": steps,
    }


def retry_available(runner: Runner | None = None) -> bool:
    """Does this install speak ``kanban retry``? The verb is overlay patch
    0002 (upstream #149556 open as of 2026-11-05), so a stock-Hermes install
    must not offer the button at all (card t_1e9354dd).

    One cheap ``kanban retry --help`` answers: rc 0 is available. An unknown
    verb (argparse rc 2), a missing binary or a timeout all answer False — the
    probe never raises, because a raising probe would take the parked-cards
    section down on a host that is merely running stock Hermes. The runner is
    resolved per call like every other call site in this module.
    """
    run = runner if runner is not None else _default_runner()
    argv = [os.environ.get("HERMES_BIN", "hermes"), "kanban", "retry", "--help"]
    try:
        done = run(argv, capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 - FileNotFoundError/timeout/OSError all mean "no verb"
        return False
    return getattr(done, "returncode", 1) == 0


def _default_runner() -> Runner:
    """Resolved per call, never as a parameter default: a test that patches
    ``subprocess.run`` must not be bypassed by an import-time binding."""
    import subprocess

    return subprocess.run
