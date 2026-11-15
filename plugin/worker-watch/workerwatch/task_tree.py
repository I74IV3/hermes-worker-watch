"""The structure layer: ``task_links`` edges -> parents, depths, render order.

Split out of ``worker_watch.py`` in this package (the split ADR-0005 recorded as its
own consequence) so the watcher's row builder stays under the module cap. Every
function here is pure over data already read from a board DB: the DB handle is
only used to read the ``task_links`` table, and the read-only connection rule
(I8) belongs to the caller that opened it.

Four shapes come out of the same edges:

* ``parents_of`` + ``depths`` — the one-parent-per-card view every table row
  carries (``parent_id`` / ``depth``), so the UI can indent a card inside its
  board group.
* ``link_edges`` + ``layer_depths`` — the graph view: every edge kept, each
  node's layer from a BFS over them.
* ``tree_order`` — rows in render order: roots first, children under parents.
* ``board_groups`` — the two sections of the snapshot grouped by board.

A board whose schema predates ``task_links`` reads as a board with no links, a
self-edge is not a parent, and a cycle never spins: depth is always finite.
"""

from __future__ import annotations

import sqlite3
from collections import deque
from collections.abc import Iterable, Sequence
from typing import Any


def parents_of(con: sqlite3.Connection) -> dict[str, str]:
    """``child_id -> parent_id`` for this board's ``task_links`` edges.

    One parent per card keeps the rendered tree a tree: when a card has several
    parents (the real schema allows it) the lowest parent id wins, so the scan
    is deterministic. A self-edge is dropped, and a board whose DB predates the
    table reads as a board with no links rather than a broken board.
    """
    try:
        edges = con.execute(
            "select parent_id, child_id from task_links order by parent_id, child_id"
        ).fetchall()
    except sqlite3.Error:
        return {}
    parents: dict[str, str] = {}
    for parent, child in edges:
        if parent == child or child in parents:
            continue
        parents[child] = parent
    return parents


def depths(parents: dict[str, str]) -> dict[str, int]:
    """Depth of every linked card: a root is 0, a child is its parent's + 1.

    Unknown parents (a done or archived card that is not in the row set) count
    as roots, and a cycle is cut at the card that closes it — depth must always
    be a finite number, since the UI indents by it and the scan must not hang.
    """
    found: dict[str, int] = {}
    for child in parents:
        chain: list[str] = []
        node = child
        while node in parents and node not in found and node not in chain:
            chain.append(node)
            node = parents[node]
        depth = found.get(node, 0) + 1
        for linked in reversed(chain):
            found[linked] = depth
            depth += 1
    return found


def link_edges(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every distinct ``task_links`` edge on this board, self-edges dropped.

    The graph view keeps them all — ``parents_of`` flattens the same table to
    one parent per card because a table row needs one place in a tree, while the
    graph *is* the edges. Sorted by ``(parent, child)`` so two scans of one board
    draw the same picture, and a board whose DB predates the table reads as a
    board with no links. A row with no id at one end — possible on a DB that
    predates the NOT NULL columns, and the reason the filter runs before the
    sort — is no edge, and sorting mixed types would raise outside
    ``sqlite3.Error`` and take the whole payload down with it.
    """
    try:
        rows = con.execute(
            "select parent_id, child_id from task_links order by parent_id, child_id"
        ).fetchall()
    except sqlite3.Error:
        return []
    return sorted(
        {
            (parent, child)
            for parent, child in rows
            if parent is not None and child is not None and parent != child
        }
    )


def _bfs_layers(seeds: Iterable[str], kids: dict[str, list[str]], layers: dict[str, int]) -> None:
    """Grow ``layers`` outward from ``seeds`` (each seeded at 0), in FIFO order.

    A node that already has a layer is never re-entered — that visited set is
    what makes a link cycle terminate instead of spinning, and it is why the
    first parent to reach a card decides its layer. Seed one node per component:
    seeding a whole list at once marks every child as placed before it is ever
    popped, which flattens the graph below the seeds onto one layer.
    """
    queue: deque[str] = deque()
    for node in seeds:
        if node in layers:
            continue
        layers[node] = 0
        queue.append(node)
    while queue:
        node = queue.popleft()
        for child in sorted(kids.get(node, [])):
            if child in layers:
                continue
            layers[child] = layers[node] + 1
            queue.append(child)


def _seed_leftovers(nodes: list[str], kids: dict[str, list[str]], layers: dict[str, int]) -> None:
    """Place whatever the roots never reached, one component at a time.

    Nothing can reach the cards of a link cycle, so each unreached node is seeded
    at 0 and the walk grows from there — the cycle breaks at its first card and
    the cards below it still hang below it. Each pass places its seed, which was
    picked precisely because it has no layer yet, so the number of unplaced nodes
    strictly drops and the loop ends. Seeding the whole leftover list at once
    would instead mark every child as placed before it was popped and flatten the
    graph below the cycle onto one layer.
    """
    for node in nodes:
        if node not in layers:
            _bfs_layers([node], kids, layers)


def layer_depths(node_ids: Iterable[str], edges: Sequence[tuple[str, str]]) -> dict[str, int]:
    """Layer of every node for the graph layout: BFS from the roots, visited set.

    A root is a node nothing points at — layer 0 — and each child sits one below
    the parent that reaches it first, so a card with two parents lands under its
    nearest one. Cards in a cycle with no root above them are never reached from
    a root, so ``_seed_leftovers`` starts each such component at 0: the layout has
    to place every card, and a finite layer for a corrupt graph beats a hung
    request. A card in no edge at all is a root of its own, also 0.
    """
    kids: dict[str, list[str]] = {}
    pointed_at: set[str] = set()
    for parent, child in edges:
        kids.setdefault(parent, []).append(child)
        pointed_at.add(child)
    nodes = sorted(node_ids)
    layers: dict[str, int] = {}
    _bfs_layers((node for node in nodes if node not in pointed_at), kids, layers)
    _seed_leftovers(nodes, kids, layers)
    return layers


def tree_order(rows: list[dict[str, Any]], *, key: Any) -> list[dict[str, Any]]:
    """One board's rows in render order: roots first, each child under its parent.

    Siblings sort by ``key`` (task id for running cards, session recency for
    history), and a card whose parent is not part of this row set — filtered
    out, done, archived — starts a rendered subtree while keeping its real
    ``depth``, so the indent still says where the card sits on the board. A link
    cycle has no root to hang it on, so its cards are appended at the end:
    a broken graph must never make a card disappear from the table.
    """
    present = {r["task_id"] for r in rows}
    roots: list[dict[str, Any]] = []
    kids: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        parent = row["parent_id"]
        if parent is not None and parent in present:
            kids.setdefault(parent, []).append(row)
        else:
            roots.append(row)
    roots.sort(key=key)
    for siblings in kids.values():
        siblings.sort(key=key)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    stack = list(reversed(roots))
    while stack:
        row = stack.pop()
        if row["task_id"] in seen:
            continue
        seen.add(row["task_id"])
        out.append(row)
        stack.extend(reversed(kids.get(row["task_id"], [])))
    out.extend(sorted((r for r in rows if r["task_id"] not in seen), key=key))
    return out


def _running_key(row: dict[str, Any]) -> str:
    return row["task_id"]


def _history_key(row: dict[str, Any]) -> tuple[int, str]:
    return (-(row["session_at"] or 0), row["task_id"])


def board_groups(running: list[dict[str, Any]], recent: list[dict[str, Any]]) -> list[Any]:
    """Group both sections by board, each group's rows in tree order.

    Only boards holding at least one row of either section get a group; the
    section lists inside a group may be empty (a board with nothing running
    still has history worth resuming).
    """
    by_board: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for section, rows in (("running", running), ("recent", recent)):
        for row in rows:
            group = by_board.setdefault(row["board"], {"running": [], "recent": []})
            group[section].append(row)
    return [
        {
            "board": slug,
            "running": tree_order(parts["running"], key=_running_key),
            "recent": tree_order(parts["recent"], key=_history_key),
        }
        for slug, parts in sorted(by_board.items())
    ]
