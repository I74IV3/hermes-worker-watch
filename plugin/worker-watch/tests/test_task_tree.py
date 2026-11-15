"""Contract tests for workerwatch.task_tree (the task_links structure layer).

The tree pins moved here from tests/test_worker_watch.py in the same commit that
moved the code (ADR-0005's split consequence). The row-level pins of the same
behaviour — parent_id/depth on a running or history row, the schema-drift and
multi-parent rules — stay in tests/test_worker_watch.py, because they are
watched-row contracts; the tests here pin the pure functions, including the
graph layer the relationship view reads (ADR-0006).
"""

from __future__ import annotations

import sqlite3

from tests.test_worker_watch import _board, _terminates_within
from workerwatch import task_tree
from workerwatch.worker_watch import connect_ro


def test_a_link_cycle_terminates_instead_of_hanging() -> None:  # type: ignore[no-untyped-def]
    """The chain guard inside `depths` is load-bearing: remove it and a
    `task_links` cycle spins, which hangs the gate instead of failing it."""
    parents = {"t_a": "t_b", "t_b": "t_a", "t_deep": "t_a"}
    with _terminates_within(2.0):
        found = task_tree.depths(parents)
    assert set(found) == set(parents), "every linked card gets a depth"
    assert all(isinstance(depth, int) and depth >= 0 for depth in found.values())


# --- the graph layer: link_edges + layer_depths ------------------------------


def test_layer_depths_layers_a_chain_breadth_first() -> None:  # type: ignore[no-untyped-def]
    nodes = ["t_root", "t_a", "t_b", "t_leaf"]
    edges = [("t_root", "t_a"), ("t_root", "t_b"), ("t_a", "t_leaf")]
    assert task_tree.layer_depths(nodes, edges) == {
        "t_root": 0,
        "t_a": 1,
        "t_b": 1,
        "t_leaf": 2,
    }


def test_a_card_with_two_parents_sits_under_the_nearest_one() -> None:  # type: ignore[no-untyped-def]
    """BFS reaches the card from its shallow parent first, so the deep branch
    (root -> mid -> deep -> card) cannot push it three layers down."""
    nodes = ["t_root", "t_mid", "t_deep", "t_card"]
    edges = [("t_root", "t_mid"), ("t_root", "t_card"), ("t_mid", "t_deep"), ("t_deep", "t_card")]
    assert task_tree.layer_depths(nodes, edges)["t_card"] == 1


def test_the_visited_set_cuts_a_link_cycle_and_places_every_node() -> None:  # type: ignore[no-untyped-def]
    """Without the visited set this spins forever, so the pin runs under the same
    watchdog as every other cycle pin: a hung gate is worse than a red one. Cards
    inside a cycle have no root above them, so the recovery pass starts the
    component at its first node and breaks the cycle there — the exact layers are
    pinned because a range check would pass even if every card collapsed to 0."""
    nodes = ["t_a", "t_b", "t_c"]
    edges = [("t_a", "t_b"), ("t_b", "t_c"), ("t_c", "t_a")]
    with _terminates_within(2.0):
        found = task_tree.layer_depths(nodes, edges)
    assert found == {"t_a": 0, "t_b": 1, "t_c": 2}


def test_cards_below_a_cycle_hang_below_it_instead_of_collapsing_to_zero() -> None:  # type: ignore[no-untyped-def]
    """The recovery pass seeds one unreached node at a time and lets BFS grow from
    it. Seeding every unreached node at once puts their children in the layer map
    before they are popped, so a chain below a cycle lands in a single column and
    the drawing has to point its arrows backwards."""
    nodes = ["t_a", "t_b", "t_t1", "t_t2", "t_t3", "t_r"]
    edges = [
        ("t_a", "t_b"),
        ("t_b", "t_a"),
        ("t_b", "t_t1"),
        ("t_t1", "t_t2"),
        ("t_t2", "t_t3"),
    ]
    with _terminates_within(2.0):
        found = task_tree.layer_depths(nodes, edges)
    assert found == {"t_r": 0, "t_a": 0, "t_b": 1, "t_t1": 2, "t_t2": 3, "t_t3": 4}


def test_a_node_in_no_edge_is_its_own_root() -> None:  # type: ignore[no-untyped-def]
    """Orphan handling at the layout layer: an unlinked card gets layer 0, and a
    node mentioned by no edge still gets a layer — the layout has to place it."""
    assert task_tree.layer_depths(["t_alone", "t_a", "t_b"], [("t_a", "t_b")]) == {
        "t_alone": 0,
        "t_a": 0,
        "t_b": 1,
    }


def test_link_edges_drops_self_edges_and_keeps_every_parent(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The graph keeps both parents of a multi-parent card (a table row cannot)
    and reads a self-edge as no edge at all, so a card is never its own parent in
    the drawing. The order is deterministic, so two scans draw the same picture."""
    db = _board(
        tmp_path,
        "b",
        tasks=[
            ("t_a", "a", None, "done", 1, None),
            ("t_p", "p", None, "done", 1, None),
            ("t_c", "c", None, "done", 1, None),
        ],
        links=[("t_a", "t_c"), ("t_p", "t_c"), ("t_c", "t_c")],
    )
    con = connect_ro(db)
    try:
        assert task_tree.link_edges(con) == [("t_a", "t_c"), ("t_p", "t_c")]
    finally:
        con.close()


def test_link_edges_reads_a_board_with_no_task_links_table_as_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Schema drift is a board with no edges, not a broken board."""
    db = _board(tmp_path, "old", tasks=[("t_a", "a", None, "done", 1, None)], with_task_links=False)
    con = connect_ro(db)
    try:
        assert task_tree.link_edges(con) == []
    finally:
        con.close()


def test_link_edges_ignores_a_link_row_with_a_missing_end(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A board whose `task_links` predates the NOT NULL columns can hold a NULL
    id. Sorting a set that mixes None with str raises a TypeError outside
    sqlite3.Error, so the unusable rows are dropped before the sort — an edge
    with no id at one end has no node to draw anyway."""
    db = _board(
        tmp_path,
        "drift",
        tasks=[
            ("t_a", "a", None, "done", 1, None),
            ("t_b", "b", None, "done", 1, None),
        ],
        with_task_links=False,
    )
    setup = sqlite3.connect(db)  # fixture setup only; production reads stay mode=ro
    setup.execute("create table task_links (parent_id, child_id)")
    setup.executemany(
        "insert into task_links values (?,?)",
        [(None, "t_a"), ("t_b", None), ("t_b", "t_a")],
    )
    setup.commit()
    setup.close()
    con = connect_ro(db)
    try:
        assert task_tree.link_edges(con) == [("t_b", "t_a")]
    finally:
        con.close()


def test_the_graph_layer_walks_a_real_fixture_board(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Both graph functions over one read-only connection — the same call shape
    `board_graph` makes — so a schema or ordering surprise shows up here rather
    than in the API."""
    db = _board(
        tmp_path,
        "b",
        tasks=[
            ("t_root", "root", None, "done", 1, None),
            ("t_a", "a", None, "done", 1, None),
            ("t_b", "b", None, "archived", 1, None),
        ],
        links=[("t_root", "t_a"), ("t_root", "t_b"), ("t_b", "t_a")],
    )
    con = connect_ro(db)
    try:
        edges = task_tree.link_edges(con)
        ids = [row[0] for row in con.execute("select id from tasks")]
        assert edges == [("t_b", "t_a"), ("t_root", "t_a"), ("t_root", "t_b")]
        assert task_tree.layer_depths(ids, edges) == {"t_a": 1, "t_b": 1, "t_root": 0}
    finally:
        con.close()
