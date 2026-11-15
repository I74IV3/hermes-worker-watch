"""dashboard backend for the Worker Watch tab (ADR-0002, amended by ADR-0004, ADR-0005,
ADR-0006, ADR-0007, ADR-0012; made self-contained by ADR-0018).

Thin shim: every read lives in the ``workerwatch`` package that sits beside
this ``dashboard/`` dir — ``workerwatch/worker_watch.py`` (boards),
``workerwatch/root_chats.py`` (the session store), ``workerwatch/card_detail.py``
(one card, fully) and ``workerwatch/card_restart.py`` (parked cards); this file
only wires the FastAPI router (mounted at /api/plugins/worker-watch/ by the
dashboard) and keeps the event loop free — the board and store scans are
plain-file SQLite reads, but they must never run on the loop. Six routes:
`/watch` serves one snapshot, grouped by board and tree-ordered, with `board`,
`assignee` and the repeatable `status` params as the tab's filters; `/graph`
serves one relationship graph per board (nodes + edges) for the SVG the tab
draws; `/chats` serves this host's own root chats from the session store, with
the repeatable `source` param widening the source filter on demand; `/card`
serves one card in full; `/stalled` serves the parked cards (ready + assigned +
still carrying a failure stamp) that `/watch` cannot show. Those five are GET
only.

`POST /restart` is the single write this plugin owns (ADR-0016), and it writes
nothing itself: `workerwatch/card_restart.py` runs Hermes' own `kanban retry`
for one parked card, refusing every other shape, and re-reads the row before
reporting success.

Install shape (ADR-0018): the plugin resolves its package from its own
location — the dir containing ``dashboard/`` — never from an enclosing repo and
never from an environment variable, so a catalog copy of the subdir under
~/.hermes/plugins is the whole dependency. A missing read module fails the
import loudly — the dashboard logs it and no route is mounted.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
_PKG_ROOT = _HERE.parent  # the plugin subdir: holds dashboard/ and workerwatch/
# The four modules this file imports directly get named guards; task_tree and
# workerlink are transitive imports of those and fail the same load loudly as
# ModuleNotFoundError (ADR-0018).
for _module in ("worker_watch.py", "root_chats.py", "card_detail.py", "card_restart.py"):
    if not (_PKG_ROOT / "workerwatch" / _module).is_file():
        raise RuntimeError(
            f"worker-watch dashboard plugin: no workerwatch/{_module} under {_PKG_ROOT}"
        )
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

from fastapi import APIRouter, HTTPException, Query  # noqa: E402 - package path precedes imports
from pydantic import BaseModel, Field  # noqa: E402 - same seam
from workerwatch.card_detail import card_detail  # noqa: E402 - same seam
from workerwatch.card_restart import (  # noqa: E402 - same seam
    restart_card,
    retry_available,
    stalled_snapshot,
)
from workerwatch.root_chats import (  # noqa: E402 - same seam
    ROOT_CHAT_CAP,
    ROOT_SOURCES,
    root_chat_snapshot,
)
from workerwatch.worker_watch import (  # noqa: E402 - same seam
    HISTORY_CAP,
    WATCH_STATUSES,
    graph_snapshot,
    watch_snapshot,
)

router = APIRouter()

# A source is a short slug from the store's `source` column, not free text. The
# vocabulary belongs to Hermes, so it is not an allow-list — a source this code
# was written before still works — but an empty or runaway value is a typo, and a
# typo must not read as "no chats".
SOURCE_MAX_LEN = 32


@router.get("/watch")
async def watch(
    limit: int = Query(HISTORY_CAP, ge=1, le=100),
    board: str | None = Query(None, max_length=64),
    assignee: str | None = Query(None, max_length=64),
    status: list[str] = Query(default_factory=list),
) -> dict[str, Any]:
    """One snapshot: running cards and recent sessions, each grouped by board
    with its cards nested parent -> child, filtered by `board`, `assignee` and
    `status`.

    `status` repeats (`?status=running&status=done`) and defaults to every
    watched status; a value outside that set is a 422, because a typo in a
    checkbox must not read as an empty board. `limit` caps the history section
    after the filters run, so one board gets its own newest cards.
    """
    unknown = sorted({s for s in status if s not in WATCH_STATUSES})
    if unknown:
        raise HTTPException(
            status_code=422,
            detail="unknown status filter: "
            + ", ".join(unknown)
            + " (watched: "
            + ", ".join(WATCH_STATUSES)
            + ")",
        )
    snapshot = await asyncio.to_thread(
        watch_snapshot,
        board=board,
        assignee=assignee,
        statuses=tuple(status) or None,
        limit=limit,
    )
    return {"generated_at": int(time.time()), **snapshot}


@router.get("/graph")
async def graph(board: str | None = Query(None, max_length=64)) -> dict[str, Any]:
    """One payload per board: its relationship graph — every card a node, every
    `task_links` edge an arrow, each node tagged with status and layer.

    `board` narrows the payload to that slug and the entry shape is unchanged, so
    the tab has one renderer either way. A board with no cards, an unknown slug,
    or a DB it cannot open simply contributes no entry. GET only, exactly like
    `/watch`: nothing on this router can write a board.
    """
    snapshot = await asyncio.to_thread(graph_snapshot, board=board)
    return {"generated_at": int(time.time()), **snapshot}


@router.get("/chats")
async def chats(
    limit: int = Query(ROOT_CHAT_CAP, ge=1, le=100),
    source: list[str] = Query(default_factory=list),
) -> dict[str, Any]:
    """This host's own chats: root sessions from the session store, newest first.

    A root chat has no parent session (so no subagent or spawned child), a human
    `source`, and is neither archived nor hidden; the store read is
    read-only (I8) and lives in `swetools/root_chats.py`. `source` repeats —
    `?source=cron&source=oneshot` adds the timer and one-shot runs on demand —
    and defaults to the human sources. It is not an allow-list: the source
    vocabulary belongs to Hermes, so an unknown source answers with no rows and
    echoes what was asked. `limit` caps the section like `/watch`'s history does,
    bounded 1..100. There is no `board` here: a chat belongs to the host, not to a
    board, and the tab's filters do not narrow it.
    """
    malformed = [s for s in source if not s.strip() or len(s) > SOURCE_MAX_LEN]
    if malformed:
        raise HTTPException(
            status_code=422,
            detail=f"a source must be 1-{SOURCE_MAX_LEN} characters, got: "
            + ", ".join(repr(s) for s in malformed),
        )
    snapshot = await asyncio.to_thread(
        root_chat_snapshot, sources=tuple(source) or ROOT_SOURCES, limit=limit
    )
    return {"generated_at": int(time.time()), **snapshot}


@router.get("/card")
async def card(
    board: str = Query(..., min_length=1, max_length=64),
    task: str = Query(..., min_length=1, max_length=64),
) -> dict[str, Any]:
    """One card, fully: facts, body, newest events and comments, and the
    admitted session's meta and token counts — what the drawer answers.

    `board` and `task` are both required: the drawer always knows which board a
    node came from, and a lookup that guesses a board would read the wrong
    DB. An unknown board, an unknown task, or an unreadable DB is a 404 — the
    drawer says "no such card" instead of drawing an empty shell. GET only,
    exactly like the rest of this router: nothing here writes a board or a
    session store (I8).
    """
    data = await asyncio.to_thread(card_detail, board, task)
    if data is None:
        raise HTTPException(status_code=404, detail=f"no card {task!r} on board {board!r}")
    return {"generated_at": int(time.time()), **data}


@router.get("/stalled")
async def stalled(board: str | None = Query(None, max_length=64)) -> dict[str, Any]:
    """Cards the dispatcher will not spawn: `ready`, assigned, and still carrying
    a failure stamp — the shape `check_respawn_guard` reads as a quota blocker.

    `/watch` cannot show these (its statuses are running/done/review/blocked), so
    a stranded card was invisible on this tab. One entry per board that has any,
    read through `swetools/card_restart.py`, GET only.

    `retry_available` rides the payload (card t_1e9354dd): `kanban retry` is an
    overlay verb, not an upstream one, so the bundle disarms every Restart
    button from this one key instead of letting a stock install POST into a CLI
    that will not answer.
    """
    snapshot, available = await asyncio.gather(
        asyncio.to_thread(stalled_snapshot, board=board),
        asyncio.to_thread(retry_available),
    )
    return {"generated_at": int(time.time()), "retry_available": available, **snapshot}


class RestartRequest(BaseModel):
    """One parked card to restart. Both fields are path segments on the board
    side, so they are bounded here and validated again in the module."""

    board: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=64)


@router.post("/restart")
async def restart(req: RestartRequest) -> dict[str, Any]:
    """Clear one parked card's stale failure stamp through Hermes' own verbs.

    The only write on this router (ADR-0016). `swetools/card_restart.py` refuses
    every shape that is not a parked card and reports the refusal with its HTTP
    class: 404 unknown card, 409 not restartable, 502 the CLI failed or the stamp
    survived. A success means the row was re-read and the stamp is gone.
    """
    result = await asyncio.to_thread(restart_card, req.board, req.task_id)
    if not result.get("ok"):
        raise HTTPException(
            status_code=int(result.get("status") or 500),
            detail=str(result.get("reason") or "restart failed"),
        )
    return {"generated_at": int(time.time()), **result}
