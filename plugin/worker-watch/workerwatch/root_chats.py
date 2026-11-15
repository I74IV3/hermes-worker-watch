"""Root chats — the human sessions on this host, read from the session store.

The third Worker Watch section (ADR-0007). Workerlink and ``worker_watch`` reach a
worker's chat through a board's ``task_events``; a *human* chat has no card to
start from, so this module reads the session store itself —
``<hermes_home>/state.db``, opened only through the read-only URI (I8) and read
with SELECTs. One row per root chat:

* no ``parent_session_id`` — a subagent run or a spawned child chat hangs below a
  root session and is not itself a root chat,
* ``source`` in ``ROOT_SOURCES`` (``local``/``tui``) — ``kanban`` and
  ``subagent`` are workers, ``cron``/``oneshot`` are timers and one-shot runs, and
  the caller may widen the set on demand,
* not ``archived`` and not ``hidden``,
* the store's ``pinned`` keep flag first (ADR-0015): a pinned chat heads the
  section however much newer the unpinned traffic gets, and pinned chats past
  the cap are back-filled after the page (capped the same way again), so a pin
  never silently drops off the list,
* newest first, capped at ``ROOT_CHAT_CAP``.

Each row keeps the same ``/chat?profile=…&resume=…`` link the card rows carry,
built by ``workerlink.chat_path`` from the session's own ``profile_name`` (NULL
profile means ``default``). A store this code cannot open — missing, corrupt, or a
schema with no ``sessions`` table — answers with no rows, the way a board that
cannot be opened answers with no cards elsewhere in the read side.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .workerlink import chat_path, hermes_home

ROOT_SOURCES: tuple[str, ...] = ("local", "tui")
ROOT_CHAT_CAP = 25
UNTITLED = "(untitled)"

# The recency key is the store's own effective activity — `last_activity_at` or,
# when that was never recorded (most `tui` rows on a live host), `started_at`.
# It is the same expression the store indexes as
# `idx_sessions_effective_activity`, so the newest chats are the newest chats.
_EFFECTIVE = "coalesce(last_activity_at, started_at)"
# Pinned reads as a 0/1 flag even where the column has drifted: a blob or text
# never sorts into the pinned block, which is exactly what `_flag` below says.
_IS_PIN = "(pinned = 1)"
_WHERE = " where parent_session_id is null and archived = 0 and hidden = 0 and source in ({marks})"
_COLS = "id, title, source, profile_name, pinned"
_QUERY = (
    f"select {_COLS}, {_EFFECTIVE} from sessions{_WHERE}"
    f" order by {_IS_PIN} desc, {_EFFECTIVE} desc, id limit ?"
)
# The page's pinned-first order only reaches the pinned rows inside the cap;
# this is the same read for pinned rows the cap left out (ADR-0015), so a pin
# never drops off the section by age alone.
_BACKFILL = (
    f"select {_COLS}, {_EFFECTIVE} from sessions{_WHERE} and pinned = 1"
    f" and id not in ({{ids}}) order by {_EFFECTIVE} desc, id limit ?"
)
# The slice an older store answers with: no `pinned` column at all. The read
# degrades to it (every row unpinned) rather than losing the whole section.
_LEGACY = (
    f"select id, title, source, profile_name, {_EFFECTIVE} from sessions{_WHERE}"
    f" order by {_EFFECTIVE} desc, id limit ?"
)


def state_db(home: Path) -> Path:
    """The session store next to the boards: ``<hermes_home>/state.db``."""
    return home / "state.db"


def connect_state_ro(db: Path) -> sqlite3.Connection:
    """The one connection shape allowed against the session store: read-only URI.

    ``timeout`` is the busy timeout: the gateway writes this store constantly, and
    a watcher must wait a moment for the lock rather than raise or write.
    """
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)


def _activity(value: Any) -> float | int | None:
    """The effective time as a JSON-safe number, or ``None``.

    A drifted or foreign store can hold text or a blob in a time column. Bytes
    would blow up the response encoder outside ``sqlite3.Error`` — one bad column
    taking the whole section down — and text would print as raw characters in the
    UI's relative-time cell, so neither is passed through.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _flag(value: Any) -> bool:
    """The ``pinned`` column as a plain bool.

    Same discipline as ``_activity`` and ``_tokens``: a drifted store can hold
    text or a blob in the flag column, and only a real integer ``1`` counts as
    pinned — anything else reads as unpinned rather than reaching the payload
    raw.
    """
    return isinstance(value, int) and value != 0


def _row(
    session_id: Any,
    title: Any,
    source: Any,
    profile_name: Any,
    pinned: Any,
    activity_at: Any,
) -> dict[str, Any] | None:
    """One chat row, or ``None`` for a store row with no session id to resume."""
    if not isinstance(session_id, str) or not session_id:
        return None
    # a blank profile_name is as unusable as a missing one: the resume link
    # would ask the dashboard to resume in an empty profile
    profile = profile_name if isinstance(profile_name, str) and profile_name else "default"
    return {
        "session_id": session_id,
        # the fallback lives here, so the UI never has to guess what a blank or
        # missing title means and every row reads with a name
        "title": title if isinstance(title, str) and title else UNTITLED,
        "source": source,
        "profile": profile,
        # the key is always present — a store without the pinned column
        # answers unpinned (below), it does not drop the key
        "pinned": _flag(pinned),
        "last_activity_at": _activity(activity_at),
        "chat_link": chat_path(profile, session_id),
    }


def _rows(found: Any, *, unpinned: bool = False) -> list[dict[str, Any]]:
    """Store rows kept as chat rows, skipping any with no session id.

    ``unpinned=True`` is the legacy shape: the select carried no ``pinned``
    column, so every row is built with an explicit unpinned flag.
    """
    if unpinned:
        return [
            row
            for row in (_row(sid, title, src, prof, 0, eff) for sid, title, src, prof, eff in found)
            if row is not None
        ]
    return [row for row in (_row(*row_tuple) for row_tuple in found) if row is not None]


def _tokens(value: Any) -> int | None:
    """A token count as a JSON-safe int, or ``None``.

    Same discipline as ``_activity``: a drifted store can hold text or a blob in
    a token column, and one bad cell must not take the response encoder down.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _usable_ids(session_ids: Any) -> Any:
    """An iterable the lookup can walk: anything list-shaped passes through, a
    generator is drained, and ``None`` or a non-iterable reads as no ids at all
    — the empty short-circuit, not a ``TypeError`` at the call site."""
    if isinstance(session_ids, (list, tuple, set, frozenset)):
        return session_ids
    if session_ids is None:
        return []
    try:
        return list(session_ids)
    except TypeError:
        return []


def token_usage(session_ids: Any, home: Path | None = None) -> dict[str, dict[str, int | None]]:
    """``session_id -> {in, out, cache_read, cache_write, reasoning}`` for the
    ids the store knows.

    The five counts answer the "what did this cost" question honestly: a worker
    whose prompt re-read a large cache every turn shows it beside the paid
    in/out, and a thinking model shows its reasoning tokens. Unknown ids are
    simply absent; a missing, corrupt or drifted store answers with nothing. The
    empty case returns before touching the store — most snapshot calls have
    sessions, but a filtered-out one has none and must not pay for (or fail on)
    a connection.
    """
    ids = list(dict.fromkeys(s for s in _usable_ids(session_ids) if isinstance(s, str) and s))
    if not ids:
        return {}
    try:
        con = connect_state_ro(state_db(home or hermes_home()))
    except sqlite3.Error:
        return {}
    try:
        found = con.execute(
            "select id, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, reasoning_tokens from sessions where id in "
            f"({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
    except sqlite3.Error:
        # an older store slice without the token columns keeps working: no
        # tokens beats no section
        return {}
    finally:
        con.close()
    return {
        sid: {
            "in": _tokens(tin),
            "out": _tokens(tout),
            "cache_read": _tokens(cread),
            "cache_write": _tokens(cwrite),
            "reasoning": _tokens(reas),
        }
        for sid, tin, tout, cread, cwrite, reas in found
        if isinstance(sid, str)
    }


def attach_tokens(entries: list[dict[str, Any]], home: Path | None = None) -> list[dict[str, Any]]:
    """Add ``tokens_in`` / ``tokens_out`` / ``tokens_cache_read`` /
    ``tokens_cache_write`` / ``tokens_reasoning`` to every entry in place.

    One lookup per batch, resolved or ``None`` — a row with no session, or a
    session the store no longer lists, reads as no tokens rather than a hole in
    the payload shape.
    """
    usage = token_usage([e.get("session_id") for e in entries], home)
    for entry in entries:
        u = usage.get(entry.get("session_id") or "", {})
        entry["tokens_in"] = u.get("in")
        entry["tokens_out"] = u.get("out")
        entry["tokens_cache_read"] = u.get("cache_read")
        entry["tokens_cache_write"] = u.get("cache_write")
        entry["tokens_reasoning"] = u.get("reasoning")
    return entries


def session_meta(session_id: Any, home: Path | None = None) -> dict[str, Any] | None:
    """What one session was, for the card drawer: title, model, source, counts,
    and the store's own description of its last activity.

    The same fail-closed rules as every other read here: a missing or drifted
    store, or an id the store does not list, answers ``None`` — the drawer keeps
    its board facts and simply has no session to show. The read is read-only
    (I8), and this module remains the only one that opens the store.
    """
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        con = connect_state_ro(state_db(home or hermes_home()))
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "select title, model, source, profile_name, message_count,"
            " tool_call_count, last_activity_description from sessions where id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    title, model, source, profile, msgs, tools, activity = row
    profile = profile if isinstance(profile, str) and profile else "default"
    return {
        "title": title if isinstance(title, str) and title else UNTITLED,
        "model": model if isinstance(model, str) and model else None,
        "source": source if isinstance(source, str) else None,
        "message_count": _tokens(msgs),
        "tool_call_count": _tokens(tools),
        "last_activity_description": activity if isinstance(activity, str) and activity else None,
        "profile": profile,
        "chat_link": chat_path(profile, session_id),
    }


def _pinned_first(con: sqlite3.Connection, sources: tuple[str, ...], limit: int) -> list[dict]:
    """The page (pinned chats first, then newest), plus the pinned rows the
    cap left out — ADR-0015, the same promise the store's own listings make
    when they back-fill pinned rows past their limit."""
    marks = ",".join("?" for _ in sources)
    kept = _rows(con.execute(_QUERY.format(marks=marks), (*sources, limit)).fetchall())
    if len(kept) < limit:
        # the page already holds every row the store has for these sources:
        # there is nothing past the cap to back-fill
        return kept
    page_ids = [row["session_id"] for row in kept]
    extra = con.execute(
        _BACKFILL.format(marks=marks, ids=",".join("?" for _ in page_ids)),
        (*sources, *page_ids, limit),
    ).fetchall()
    return kept + _rows(extra)


def root_chats(
    home: Path | None = None,
    *,
    sources: tuple[str, ...] = ROOT_SOURCES,
    limit: int = ROOT_CHAT_CAP,
) -> list[dict[str, Any]]:
    """This host's root chats: pinned first, then newest activity.

    ``sources`` is the source column filter — pass ``ROOT_SOURCES + ("cron",
    "oneshot")`` to see the timer and one-shot runs too. An unknown source simply
    matches nothing; an empty one selects nothing without touching the store.
    ``limit`` caps the rows (``ROOT_CHAT_CAP`` by default, ``limit <= 0`` selects
    nothing — a negative SQLite ``LIMIT`` would mean *unlimited*, which is not
    what a cap means); the pinned back-fill of ADR-0015 answers at most
    ``limit`` rows of its own, so the section stays bounded.
    """
    if not sources or limit <= 0:
        return []
    try:
        con = connect_state_ro(state_db(home or hermes_home()))
    except sqlite3.Error:
        return []
    try:
        try:
            kept = _pinned_first(con, sources, limit)
        except sqlite3.Error:
            # an older store slice without the pinned column keeps working:
            # no pins beats no section, the way a store without the token
            # columns keeps its rows with no tokens
            legacy = _LEGACY.format(marks=",".join("?" for _ in sources))
            kept = _rows(con.execute(legacy, (*sources, limit)).fetchall(), unpinned=True)
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return attach_tokens(kept, home)


def root_chat_snapshot(
    home: Path | None = None,
    *,
    sources: tuple[str, ...] = ROOT_SOURCES,
    limit: int = ROOT_CHAT_CAP,
) -> dict[str, Any]:
    """The section in one payload: the sources and cap it selected, then the rows.

    The filters are echoed because a source that matches nothing is otherwise
    indistinguishable from a host with no chats.
    """
    return {
        "filters": {"sources": list(sources), "limit": limit},
        "chats": root_chats(home, sources=sources, limit=limit),
    }
