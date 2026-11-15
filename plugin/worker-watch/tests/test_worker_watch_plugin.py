"""plugin/worker-watch — the Worker Watch dashboard tab contract (ADR-0002).

The dashboard loads plugin_api.py directly, so these pins cover the install
shape: manifest fields, bundle registration, and an E2E GET of the mounted
route through FastAPI's TestClient (no network, no live boards — HERMES_HOME
points at a fixture board root). Read-only is pinned on BOTH sides: the router
exposes GET only, and the bundle never issues a write request.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.test_root_chats import _state
from tests.test_worker_watch import _board

HERE = Path(__file__).resolve().parent  # plugin/worker-watch/tests
REPO = HERE.parents[2]  # the devtools repo root
PLUGIN = HERE.parent / "dashboard"  # plugin/worker-watch/dashboard

MOUNT = "/api/plugins/worker-watch"

# the watched statuses the API answers for when no status filter is sent
ALL_STATUSES = ("running", "done", "review", "blocked")


def load_plugin_api(monkeypatch: pytest.MonkeyPatch):
    """Import plugin_api.py fresh from the repo copy of the plugin."""
    monkeypatch.delenv("DEVTOOLS_REPO", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    return _load_from(PLUGIN / "plugin_api.py")


def _load_from(path: Path):
    """Exec plugin_api.py at ``path`` as a fresh module (no sys.modules entry)."""
    spec = importlib.util.spec_from_file_location("worker_watch_plugin_api", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_client(mod) -> TestClient:
    app = FastAPI()
    app.include_router(mod.router, prefix=MOUNT)
    return TestClient(app)


def test_manifest_shape_and_files() -> None:
    manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "worker-watch"
    assert manifest["label"] == "Worker Watch"
    assert manifest["tab"]["path"] == "/worker-watch"
    assert (PLUGIN / manifest["entry"]).is_file()
    assert (PLUGIN / manifest["api"]).is_file()


def test_bundle_registers_and_is_read_only() -> None:
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    assert 'window.__HERMES_PLUGINS__.register("worker-watch"' in bundle
    # one snapshot route; the filter bar builds its query string onto it
    assert 'var WATCH_URL = "/api/plugins/worker-watch/watch"' in bundle
    assert "fetchJSON(watchUrl(filters))" in bundle
    assert 'params.set("board", filters.board)' in bundle
    assert 'params.set("assignee", filters.assignee)' in bundle
    assert 'params.append("status", s)' in bundle
    # grouped by board, indented by the card's depth, resume link on every row
    assert "data.boards" in bundle
    assert '"data-depth": r.depth' in bundle
    assert "href: r.chat_link" in bundle
    # the graph half (ADR-0006): second GET route, own query builder, and the
    # SVG surface the render harness walks
    assert 'var GRAPH_URL = "/api/plugins/worker-watch/graph"' in bundle
    assert "fetchJSON(graphUrl({ board: filters.board }))" in bundle
    assert '"data-graph": graph.board' in bundle
    assert '"data-node": n.id' in bundle
    assert '"data-unlinked": n.id' in bundle
    assert 'h("svg"' in bundle
    # a graph read that fails keeps the picture it already drew: nothing on this
    # path may blank graphData, and the graph never composes a /chat href itself
    assert "setGraphData(null)" not in bundle
    assert ".catch(function () { /* keep the last good graph */ });" in bundle
    # the layout hangs each card on its nearest parent and reads a board status
    # only from the colour table — the two rules the render harness pins by fact
    assert "nearestParent(parents[n.id] || [], byId)" in bundle
    assert "Object.prototype.hasOwnProperty.call(STATUS_FILL, status)" in bundle
    # edges are classified, coloured, and clickable (ADR-0008): the marked edge
    # and hit paths, the child-in-the-parent's-own-layer-or-above rule, and the
    # one-edge selection the TAB holds — not the per-board graph renderer, which
    # is called as a plain function per board and must not own hooks
    assert '"data-edge": label' in bundle
    assert '"data-edge-dir"' in bundle
    assert '"data-edge-hit"' in bundle
    assert "edgeIsBack(byId[e.parent], byId[e.child])" in bundle
    assert "nextEdge(props.selected, item.edge)" in bundle
    assert "selected: selectedEdge" in bundle
    # click-to-detail (ADR-0011, widened by ADR-0012): the node selection
    # toggles like the edge one, every node and chip carries it, and the
    # drawer — not the picture — holds the resume anchor
    assert "nodeClicked(byId[p.id])" in bundle
    assert "onClick: nodeClicked(n)," in bundle
    assert '"data-summary": graph.board' in bundle
    assert 'h("a", { href: n.chat_link, className: "underline" }' in bundle
    assert "selectedNode: selectedNode" in bundle
    # the drawer (ADR-0012): the tab reads the whole card per selection, the
    # side window is keyed to the board and node, resolves against the live
    # payload, and marks its loading line and labelled token breakdown
    assert 'var CARD_URL = "/api/plugins/worker-watch/card"' in bundle
    assert 'params.set("task", task)' in bundle
    assert "fetchJSON(cardUrl(selectedNode.board, selectedNode.id))" in bundle
    assert "var drawerFound = findNode(graphs, selectedNode);" in bundle
    assert "if (!found) return null;" in bundle
    assert '"data-detail": graph.board + "/" + node.id' in bundle
    assert '"data-detail-loading": "1"' in bundle
    assert 'add("cache read", r.tokens_cache_read);' in bundle
    # the root-chats half (ADR-0007): third GET route, its own query builder, and
    # the table the render harness walks, keyed by session id rather than task id
    assert 'var CHATS_URL = "/api/plugins/worker-watch/chats"' in bundle
    assert "fetchJSON(chatsUrl())" in bundle
    assert "RootChats({ chats: chatsOf(chatData) })" in bundle
    assert '["last_activity_at", "Last activity", "ago", "18%"]' in bundle
    assert 'keyField: "session_id"' in bundle
    # the shared column grid + coloured status cells (ADR-0009): one fixed
    # grid per table kind so columns align across sections and board cards,
    # and the status palette dots the graph already used now speak in the
    # table cells too — the mapping the user pinned
    assert '"w-full text-sm table-fixed"' in bundle
    assert 'h("colgroup", null,' in bundle
    assert '["status", "Status", "status", "10%"]' in bundle
    assert '"data-status-dot": label' in bundle
    assert 'running: "#3b82f6"' in bundle
    assert 'review: "#facc15"' in bundle
    # the Tokens column (ADR-0010): the payload's store-resolved counts, one
    # compact format, and a marked cell in every row so a dropped column is
    # visible to the render harness rather than a silent blank
    assert '["tokens", "Tokens in/out", "tokens", "18%"]' in bundle
    assert '"data-tokens": shown' in bundle
    assert "function fmtTokens(n)" in bundle
    # a chats read that fails keeps the rows it already drew
    assert "setChatData(null)" not in bundle
    assert ".catch(function () { /* keep the last good chats */ });" in bundle
    # and it rides the same refresh as the other two sections: first paint,
    # interval, and the Rescan button all call it
    assert "load();\n      loadGraph();\n      loadChats();\n      loadStalled();" in bundle
    assert (
        "setInterval(function () { load(); loadGraph(); loadChats(); loadStalled(); }, REFRESH_MS)"
        in bundle
    )
    assert "onClick: function () { load(); loadGraph(); loadChats(); loadStalled(); }," in bundle
    # ADR-0013: one more read and the ONE write this tab owns. The read lists the
    # parked cards `/watch` cannot show (ready + assigned + stamped); the write is
    # a single POST to the pinned URL with the two identifiers and nothing else.
    assert 'var STALLED_URL = "/api/plugins/worker-watch/stalled"' in bundle
    assert 'var RESTART_URL = "/api/plugins/worker-watch/restart"' in bundle
    assert "fetchJSON(stalledUrl(filters))" in bundle
    assert 'params.set("board", filters.board)' in bundle
    assert "function stalledUrl(filters)" in bundle
    assert "stalledOf(stalledData)" in bundle
    assert "fetchJSON(RESTART_URL, {" in bundle
    assert "body: JSON.stringify({ board: board, task_id: taskId })," in bundle
    assert '"data-restart": key' in bundle
    assert '"data-restart-msg": key' in bundle
    assert '"data-stalled-row": key' in bundle
    assert '"data-restart-log": props.log.length' in bundle
    # the Restart button follows the probe flag through one prop and one guard,
    # and the disarmed placeholder carries its own marker (ADR-0016)
    assert "retryAvailable: stalledRetryOn(stalledData)," in bundle
    assert "props.retryAvailable === true" in bundle
    assert '"data-restart-off": key' in bundle
    assert "function stalledRetryOn(payload)" in bundle
    # the pinned-chats strip (ADR-0017): its markers, the strict filter, and
    # the Page wiring that puts it above every table
    assert '"data-pinned-strip": chats.length' in bundle
    assert '"data-strip-row": c.session_id' in bundle
    assert "c.pinned === true" in bundle
    assert bundle.index("PinnedStrip({ chats: pinnedOf(chatData) })") < bundle.index(
        "FilterBar({ filters"
    )
    assert "stamp cleared — claimed within 60 s" in bundle
    # exactly ONE write verb in the whole bundle, and no second method: a stray
    # PUT/DELETE or a second POST would be a write surface nobody reviewed
    assert bundle.count('method: "POST"') == 1
    for verb in ('method: "PUT"', 'method: "DELETE"', 'method: "PATCH"', '"PUT"', '"DELETE"'):
        assert verb not in bundle


def test_bundle_pin_surface_exists_for_the_render_harness() -> None:
    """The harness drives the bundle through a stub SDK; without the exported
    hooks it can check nothing but syntax."""
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    assert "window.__HERMES_WORKER_WATCH__" in bundle


def _run_node_harness(tmp_path) -> str:  # type: ignore[no-untyped-def]
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    proc = subprocess.run(
        [node, str(harness), str(PLUGIN / "dist" / "index.js")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"harness failed:\n{proc.stderr}"
    return proc.stdout.strip()


def test_bundle_renders_an_indent_per_depth_and_a_resume_link_per_row(tmp_path) -> None:
    """The acceptance contract, checked without a browser: a 2-level chain
    renders 0/1/2 indents inside one board group, every row with a session keeps
    its same-origin resume href, and a session-less row keeps its placeholder.
    The graph half (ADR-0006): one SVG per board, each node placed at its layer
    with its own resume anchor, one path per edge, a status dot per node, and the
    card in no link shown in the unlinked row instead of drawn into the tree. The
    exact cells are pinned too: a card with two parents hangs on its nearest one,
    and a second parent in the card's own layer must not push the pair onto one
    row."""
    out = _run_node_harness(tmp_path)
    assert out.splitlines() == [
        "boards=devtools",
        "depths=1,0,1,2,0,0",
        "anchors=3",
        "hrefs=/chat?profile=default&resume=s0,"
        "/chat?profile=default&resume=s1,"
        "/chat?profile=default&resume=s2",
        "placeholders=3",
        "query=/api/plugins/worker-watch/watch?board=devtools&assignee=reviewer"
        "&status=running&status=done",
        "graphUrl=/api/plugins/worker-watch/graph?board=devtools",
        "graphSvg=1",
        "graphBoard=devtools",
        "graphLayers=t_root:0,t_c1:1,t_c2:1,t_a2:2,t_gchild:2",
        "graphUnlinked=t_alone",
        # 5 visible edge paths plus 5 invisible hit paths (ADR-0008): every drawn
        # edge is marked and clickable, the two links back toward a shallower
        # layer are classified and stroked in the back-edge colour, the legend
        # agrees with the drawing, and a selected edge highlights while its
        # endpoints are marked and everything else dims
        "graphPaths=10",
        "graphEdgePaths=5",
        "graphHits=5",
        "graphClicks=5",
        "graphEdgeDirs=t_root>t_c1:fwd,t_root>t_c2:fwd,t_c2>t_gchild:fwd,"
        "t_a2>t_gchild:back,t_gchild>t_c1:back",
        "graphEdgeStrokes=t_root>t_c1:currentColor,t_root>t_c2:currentColor,"
        "t_c2>t_gchild:currentColor,t_a2>t_gchild:#fb7185,t_gchild>t_c1:#fb7185",
        "graphBackEdges=2",
        "graphSelStyle=3@1",
        "graphSelDim=4",
        "graphSelNodes=2",
        "graphGhostDim=0",
        "graphGhostNodes=0",
        "edgeToggle=ok",
        "graphDots=5",
        # the picture navigates nowhere (ADR-0011): zero anchors in the graph,
        # one summary-click handler per placed node and per unlinked chip, and
        # the selected node's summary panel carries id, coloured status, title,
        # layer, parents and children from the payload's edges, the token
        # counts, and exactly one resume anchor; a stale selection renders
        # nothing, and the click toggles like the edge selection does
        "graphAnchors=0",
        "graphHrefs=",
        "graphSame=ok",
        "graphGeom=ok",
        # x grows with the layer; the two cards of layer 2 sit on distinct rows
        "graphCells=t_root:10,76,t_c1:228,54,t_c2:228,98,t_a2:446,10,t_gchild:446,98",
        # the board status "constructor" gets the fallback colour, not an
        # inherited Object member; the four watched statuses carry the pinned
        # palette (ADR-0009): running blue, done green, review yellow, blocked
        # red — the same colours the table cells show
        "graphFills=t_root:#22c55e,t_c1:#22c55e,t_c2:#facc15,t_a2:#94a3b8,t_gchild:#3b82f6",
        "graphChipFills=t_alone:#94a3b8",
        "graphNodeClicks=5",
        "graphChipClicks=1",
        # the drawer (ADR-0012): the selection opens a side window with a close
        # click, the fetched card shows body, events, comments and the session
        # half with its labelled exact token breakdown; while the card is being
        # read only the marked loading line stands in, a card the rescan
        # dropped resolves to no drawer, and a sessionless card says so while
        # marking its two placeholders and showing its block reason
        "detailPresent=1",
        "detailClose=1",
        "detailOpenedLoading=0",
        "detailLoadingLine=1",
        "detailGhost=0",
        "detailBody=Install the drift timer units and wire the check.",
        "detailEvents=completed,commented",
        "detailComments=2",
        "detailSession=1",
        "detailActivity=1",
        "detailTokens=sent 215,800 · received 113,296 · cache read 1,200,000 · "
        "cache write 30 · reasoning 55",
        "detailNoSessSession=none",
        "detailNoSessBlock=empty_result",
        "detailNoSessPlaceholders=chat,body",
        # the summary line inside the drawer carries the node's facts plus
        # exactly one resume anchor, its status dot on the palette; a selected
        # card with no session keeps its marked placeholder and no anchor
        "graphSummary=1",
        "graphSummaryText=t_gchild | running — t_gchild title | layer 2 | "
        "parents: t_c2, t_a2 | children: t_c1 | tokens: 216k/113k | resume in chat",
        "graphSummaryAnchors=1",
        "graphSummaryHrefs=/chat?profile=default&resume=s2",
        "graphSummaryDot=#3b82f6",
        "graphNoSessionSummary=1",
        "graphNoSessionAnchors=0",
        "nodeToggle=ok",
        # the root-chats table (ADR-0007): three rows, keyed by session id, two
        # resume anchors and one marked placeholder, and an empty payload that
        # still renders the section with no rows. The href fixture carries a
        # profile the row itself does not have, so a bundle that composes the
        # resume URL from row fields lands on a different href. The cell words
        # are pinned too: the title and source must reach the page, the relative
        # time is the cell that calls the SDK's timeAgo, and a row with no time
        # prints the em dash instead of a raw value.
        "chatsUrl=/api/plugins/worker-watch/chats",
        "chatRows=3",
        "chatRowKeys=s_human,s_other,s_noresume",
        "chatHeadings=Root chats — this host's own sessions: 3|Root chats — 3",
        "chatAnchors=2",
        "chatHrefs=/chat?profile=watcher&resume=s_human,/chat?profile=researcher-a&resume=s_other",
        "chatPlaceholders=1",
        "chatEmptyRows=0/0",
        "chatWantAnchors=2",
        "chatWantPlaceholders=1",
        "chatKeyFacts=",
        "chatCells=Review GPU pool work|tui|5k/250|—|resume in chat|"
        "(untitled)|local|1.2k/640|ago2000|resume in chat|"
        "a chat with no link|cron|—|ago3000|no session yet",
        "chatShapeFacts=",
        # the card tables answer with the SectionTable default key; a default that
        # moves to another field leaves every card row unkeyed
        "cardRowKeys=t_run,t_root,t_mid,t_leaf,t_blocked,t_nostatus",
        "cardKeyFacts=",
        # the column grid (ADR-0009, widened by ADR-0010): both card sections on
        # one fixed 7-column grid, the chats table on its own fixed 5-column
        # grid, every status cell carrying its palette dot beside its word, and
        # every row carrying its formatted token counts (or the marked
        # placeholder) in a marked Tokens cell
        "chatsColgroups=32%,12%,16%,18%,22%",
        "chatsFixedTables=1",
        "cardColgroups=12%,22%,10%,10%,18%,14%,14%|12%,22%,10%,10%,18%,14%,14%",
        "cardFixedTables=2",
        "cardStatusDots=running:#3b82f6,done:#22c55e,done:#22c55e,review:#facc15,blocked:#ef4444",
        "cardStatusWords=running|done|done|review|blocked",
        # the fixture row with an empty status reads as the marked placeholder:
        # a lone fallback-coloured dot would claim a status the row lacks
        "cardStatusPlaceholders=1",
        "cardCells=42",
        "cardTokens=—|5k/250|216k/113k ·1.2Mc|1.2k/640|—|—",
        # the Restart disarm (card t_1e9354dd / ADR-0016): available renders one
        # button per parked row; unavailable keeps every row and replaces every
        # button with the marked placeholder; a payload without the probe key
        # reads unavailable — four facts, never a guessed capability
        "stalledRowsOn=lora/t_parked",
        "stalledAnchorsOn=1",
        "stalledAnchorsOff=0",
        "stalledRowsOff=1",
        "stalledOffMarksOn=0",
        "stalledOffMarksOff=1",
        "retryOnFacts=true,false,false,false",
        # the pinned-chats strip (card t_04932b43 / ADR-0017): the two pinned
        # fixture chats render as id@ resume anchors (a bare ! row means the
        # anchor fell out), carry a star, vanish when nothing is pinned, and
        # only the strict boolean true flag pins a row
        "stripRows=s_human@,s_other@",
        "stripStar=1",
        "stripEmpty=null",
        "stripStrict=s_true",
    ]


def test_the_harness_pin_bites(tmp_path) -> None:
    """Negative control: a bundle that drops the resume anchors must redden the
    harness, so the pin above is not a rubber stamp."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    neutered = tmp_path / "no_links.js"
    neutered.write_text(
        bundle.replace("href: r.chat_link", 'href: ""'),
        encoding="utf-8",
    )
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    proc = subprocess.run(
        [node, str(harness), str(neutered)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "anchors=3" not in proc.stdout


def test_the_graph_harness_pins_bite(tmp_path) -> None:
    """Negative controls for the graph half, one per contract it draws: the
    layered layout disabled, the picture turned back into click-to-chat
    navigation or stripped of its summary clicks, the summary panel blanked,
    gone, or kept for a card the payload no longer has, the unlinked row gone,
    a status that reaches the prototype chain, a card hung on the wrong parent,
    the row-collision backstop removed, and the ADR-0008 edge surface taken
    away seven ways (back-edge classification, the distinct back stroke, the
    click handler, the selected-edge styling, the toggle, the stale-selection
    guard, the endpoint marks). Each must either redden the harness or change
    the facts pinned above, or that pin asserts nothing."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    pinned = _run_node_harness(tmp_path)
    mutations = {
        # every node in one layer: the layered layout stops layering
        "flat_layout.js": bundle.replace("depth * (NODE_W + GAP_X)", "0 * (NODE_W + GAP_X)"),
        # nodes stop being summary targets (ADR-0011)
        "no_node_click.js": bundle.replace("nodeClicked(byId[p.id])", "undefined"),
        # unlinked chips stop being summary targets
        "no_chip_click.js": bundle.replace("onClick: nodeClicked(n),", "onClick: undefined,"),
        # the summary panel's resume link blanks: the one fact the panel exists
        # to hand over is gone
        "summary_anchor_blank.js": bundle.replace(
            'h("a", { href: n.chat_link, className: "underline" }, " | resume in chat")',
            'h("a", { href: "", className: "underline" }, " | resume in chat")',
        ),
        # the summary panel stops rendering its marked container: invisible
        # selected node
        "summary_gone.js": bundle.replace('"data-summary": graph.board', '"data-sum": graph.board'),
        # the sessionless summary swaps its placeholder marker for a plain
        # word: the "no chat to resume" fact stops being a machine-readable one
        "summary_placeholder_unmarked.js": bundle.replace(
            '"data-placeholder": "chat" },\n            " | no session yet"',
            '"data-note": "chat" },\n            " | no session yet"',
        ),
        # the summary's status dot leaves the palette
        "summary_dot_uncolored.js": bundle.replace(
            '          background: statusFill(n.status), display: "inline-block",\n'
            "        },\n      }),\n      statusWord,",
            '          background: "#000000", display: "inline-block",\n'
            "        },\n      }),\n      statusWord,",
        ),
        # the drawer renders no marked container: the side window is invisible
        "drawer_gone.js": bundle.replace('"data-detail": graph.board', '"data-nope": graph.board'),
        # the loading line loses its marker: a mid-read card reads as a real,
        # nearly-empty card until the answer lands
        "loading_marker_gone.js": bundle.replace(
            'h("p", { className: "text-muted-foreground", "data-detail-loading": "1" },',
            'h("p", { className: "text-muted-foreground" },',
        ),
        # the session half stops marking itself: the drawer's answer to "what
        # was it doing" goes unfindable
        "session_half_unmarked.js": bundle.replace(
            '"data-detail-session": "1"', '"data-detail-session": "0"'
        ),
        # the labelled token breakdown loses a label: big numbers return, but
        # the reader cannot tell sent from received from cache
        "tokens_unlabelled.js": bundle.replace(
            'add("sent", r.tokens_in);', 'add("", r.tokens_in);'
        ),
        # the drawer keeps rendering a card the payload no longer resolves
        "no_findnode_guard.js": bundle.replace(
            "if (!found) return null;", "if (false) return null;"
        ),
        # unlinked cards vanish from the picture
        "no_unlinked_row.js": bundle.replace(
            "unlinked.map(function (n) {", "unlinked.slice(0, 0).map(function (n) {"
        ),
        # a board status walks the prototype chain instead of the colour table
        "prototype_fill.js": bundle.replace(
            "return Object.prototype.hasOwnProperty.call(STATUS_FILL, status)\n"
            "      ? STATUS_FILL[status]\n"
            '      : "#94a3b8";',
            'return STATUS_FILL[status] || "#94a3b8";',
        ),
        # a card hangs on the first parent in id order rather than the nearest
        # one: the picture still draws, but the pair moves to different cells
        "lexicographic_parent.js": bundle.replace(
            "var parent = nearestParent(parents[n.id] || [], byId);",
            "var parent = byId[(parents[n.id] || []).slice().sort()[0]];",
        ),
        # the row-collision backstop gone and every node asking for row 0: two
        # cards of one layer then occupy one cell and read as a single card
        "no_row_bump.js": bundle.replace(
            "if (floor !== undefined && p.row <= floor) p.row = floor + 1;", ";"
        ).replace("row: slot[n.id],", "row: 0,"),
        # --- the ADR-0008 edge surface, one mutation per guard the facts pin ---
        # every link drawn as a forward link: the return-edge rule stops classifying
        "all_forward.js": bundle.replace(
            "return (childNode.depth || 0) <= (parentNode.depth || 0);", "return false;"
        ),
        # return edges share the forward stroke: the colour stops saying "back"
        "back_colour.js": bundle.replace(
            'var EDGE_BACK_STROKE = "#fb7185";', 'var EDGE_BACK_STROKE = "currentColor";'
        ),
        # the hit paths stay, but lose their click handler
        "no_edge_click.js": bundle.replace(
            "onClick: function () { props.onSelect(nextEdge(props.selected, item.edge)); },",
            "onClick: null,",
        ),
        # the selection never reaches the stroke: nothing highlights or dims
        "sel_style_off.js": bundle.replace(
            "var style = edgeStyle(item.back, on, selected !== null && !on);",
            "var style = edgeStyle(item.back, false, false);",
        ),
        # clicking the selected edge again does not clear it
        "toggle_sticks.js": bundle.replace(
            "return sameEdge(prev, next) ? null : next;", "return next;"
        ),
        # a selection the rescan no longer carries keeps dimming the whole board
        "stale_sel.js": bundle.replace(
            "if (stillThere) selected = props.selected;", "selected = props.selected;"
        ),
        # the selected edge's endpoints lose their mark
        "endpoint_mark.js": bundle.replace(
            '"data-edge-node": on ? "1" : undefined', '"data-edge-node": undefined'
        ),
    }
    for name, mutated in mutations.items():
        assert mutated != bundle, f"{name} no longer finds its victim line"
        target = tmp_path / name
        target.write_text(mutated, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(target)],
            capture_output=True,
            text=True,
        )
        reddened = proc.returncode != 0
        moved = proc.stdout.strip() != pinned
        assert reddened or moved, f"{name} left the harness green and the facts unchanged"


def _fixture_home(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Boards with one running card, three session cards, decoys, and a real
    parent-child chain: ``t_done -> t_run`` and ``t_done -> t_review``."""
    _board(
        tmp_path,
        "devtools",
        tasks=[
            ("t_run", "running card", "default", "running", 100, 7),
            ("t_done", "finished card", "default", "done", 90, None),
            ("t_review", "held card", "reviewer", "review", 91, None),
            ("t_nosess", "no session", "default", "blocked", 92, None),
            ("t_old", "archived card", "default", "archived", 93, None),
        ],
        runs=[(7, "t_run", "default", 200), (8, "t_done", "researcher-a", 210)],
        events=[
            ("t_run", "owner_admitted", json.dumps({"session_id": "sess-1"}), 200),
            ("t_done", "owner_admitted", json.dumps({"session_id": "sess-2"}), 300),
            ("t_review", "owner_admitted", json.dumps({"session_id": "sess-3"}), 400),
            ("t_old", "owner_admitted", json.dumps({"session_id": "sess-0"}), 900),
        ],
        links=[("t_done", "t_run"), ("t_done", "t_review")],
    )
    _board(tmp_path, "renderpool", tasks=[("t_z", "other board card", None, "running", 50, None)])
    _board(tmp_path, "_archived", tasks=[("t_x", "archived", None, "running", 1, None)])
    bad = tmp_path / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_watch_endpoint_groups_both_sections_by_board(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)

    resp = make_client(mod).get(f"{MOUNT}/watch")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["generated_at"], int)
    assert body["filters"] == {"board": None, "assignee": None, "statuses": list(ALL_STATUSES)}
    assert body["board_options"] == ["devtools", "renderpool"]

    assert [
        (g["board"], [r["task_id"] for r in g["running"]], [r["task_id"] for r in g["recent"]])
        for g in body["boards"]
    ] == [
        ("devtools", ["t_run"], ["t_done", "t_review"]),
        ("renderpool", ["t_z"], []),
    ]

    run_row = body["boards"][0]["running"][0]
    assert run_row["chat_link"] == "/chat?profile=default&resume=sess-1"
    assert run_row["run_started_at"] == 200
    # the chain arrives as an indented tree: t_done is t_run's parent
    assert (run_row["parent_id"], run_row["depth"]) == ("t_done", 1)

    done_row = body["boards"][0]["recent"][0]
    assert done_row["status"] == "done"
    assert done_row["session_at"] == 300
    assert done_row["chat_link"] == "/chat?profile=researcher-a&resume=sess-2"
    assert [(r["task_id"], r["parent_id"], r["depth"]) for r in body["boards"][0]["recent"]] == [
        ("t_done", None, 0),
        ("t_review", "t_done", 1),
    ]


def test_watch_endpoint_board_filter(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)

    body = client.get(f"{MOUNT}/watch?board=renderpool").json()
    assert [g["board"] for g in body["boards"]] == ["renderpool"]
    assert [r["task_id"] for r in body["boards"][0]["running"]] == ["t_z"]
    # the select still lists every board, filtered scan or not
    assert body["board_options"] == ["devtools", "renderpool"]
    assert body["filters"]["board"] == "renderpool"
    assert client.get(f"{MOUNT}/watch?board=nope").json()["boards"] == []


def test_watch_endpoint_status_filter_is_multi_valued(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)

    body = client.get(f"{MOUNT}/watch?status=review&status=blocked").json()
    assert [r["task_id"] for g in body["boards"] for r in g["running"]] == []
    assert [r["task_id"] for g in body["boards"] for r in g["recent"]] == ["t_review"]
    assert body["filters"]["statuses"] == ["review", "blocked"]

    body = client.get(f"{MOUNT}/watch?status=running").json()
    assert [r["task_id"] for g in body["boards"] for r in g["recent"]] == []
    assert [r["task_id"] for g in body["boards"] for r in g["running"]] == ["t_run", "t_z"]


def test_watch_endpoint_rejects_an_unknown_status(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A typo in a checkbox must read as an error, not as an empty board."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    resp = make_client(mod).get(f"{MOUNT}/watch?status=archved")
    assert resp.status_code == 422
    assert "archved" in resp.text


def test_watch_endpoint_assignee_filter(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)

    body = client.get(f"{MOUNT}/watch?assignee=reviewer").json()
    assert [
        (g["board"], [r["task_id"] for r in g["running"]], [r["task_id"] for r in g["recent"]])
        for g in body["boards"]
    ] == [("devtools", [], ["t_review"])]
    assert body["assignee_options"] == ["default", "reviewer"]
    assert client.get(f"{MOUNT}/watch?assignee=nobody&status=running").json()["boards"] == []


def test_watch_endpoint_filters_combine_with_the_history_cap(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The cap runs on the filtered selection, so one board gets its own newest
    cards instead of losing them to another board's."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)
    body = client.get(f"{MOUNT}/watch?board=devtools&limit=1").json()
    assert [r["task_id"] for r in body["boards"][0]["recent"]] == ["t_review"]


def test_watch_endpoint_limit_caps_the_history_section(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)
    body = client.get(f"{MOUNT}/watch?limit=1").json()
    assert [r["task_id"] for g in body["boards"] for r in g["recent"]] == ["t_review"]
    assert client.get(f"{MOUNT}/watch?limit=0").status_code == 422
    assert client.get(f"{MOUNT}/watch?limit=500").status_code == 422


def test_route_is_get_only(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    client = make_client(mod)
    assert client.post(f"{MOUNT}/watch").status_code == 405
    assert client.get(f"{MOUNT}/running").status_code == 404
    # ADR-0005: the filters ride the one `/watch` snapshot — no second route
    assert client.get(f"{MOUNT}/sessions").status_code == 404
    # ADR-0006: `/graph` is the second GET route, and it is GET-only too
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(client, verb)(f"{MOUNT}/graph").status_code == 405
    assert client.get(f"{MOUNT}/graphs").status_code == 404
    # ADR-0007: `/chats` is the third GET route — GET-only, and the singular is
    # not a route, so a typo cannot quietly read as an empty section
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(client, verb)(f"{MOUNT}/chats").status_code == 405
    assert client.get(f"{MOUNT}/chat").status_code == 404
    # ADR-0012: `/card` is the fourth GET route — GET-only, and the plural is
    # not a route
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(client, verb)(f"{MOUNT}/card").status_code == 405
    assert client.get(f"{MOUNT}/cards").status_code == 404


# --- the /graph endpoint (ADR-0006) ------------------------------------------


def test_graph_endpoint_serves_one_graph_per_board(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The payload the SVG draws: one entry per board, each with `nodes` (id,
    title, status, depth, session_id, resume link) and `edges` (parent, child).
    The fixture's chain is `t_done -> t_run` plus `t_done -> t_review`."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)

    resp = make_client(mod).get(f"{MOUNT}/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["generated_at"], int)
    assert body["filters"] == {"board": None}
    assert [g["board"] for g in body["boards"]] == ["devtools", "renderpool"]

    graph = body["boards"][0]
    assert graph["edges"] == [
        {"parent": "t_done", "child": "t_review"},
        {"parent": "t_done", "child": "t_run"},
    ]
    assert [(n["id"], n["depth"]) for n in graph["nodes"]] == [
        ("t_done", 0),
        ("t_nosess", 0),
        ("t_old", 0),
        ("t_review", 1),
        ("t_run", 1),
    ]
    run_node = next(n for n in graph["nodes"] if n["id"] == "t_run")
    assert run_node["status"] == "running"
    assert run_node["title"] == "running card"
    assert run_node["chat_link"] == "/chat?profile=default&resume=sess-1"
    # an archived card and a session-less card are still nodes: the graph shows
    # the board's structure, and a node with no session just has no click target
    assert next(n for n in graph["nodes"] if n["id"] == "t_old")["status"] == "archived"
    alone = next(n for n in graph["nodes"] if n["id"] == "t_nosess")
    assert alone["session_id"] is None and alone["chat_link"] is None


def test_graph_endpoint_board_filter(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`?board=` narrows the payload to one entry and changes nothing else; an
    unknown slug answers with no entries rather than an error, matching `/watch`."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)

    body = client.get(f"{MOUNT}/graph?board=renderpool").json()
    assert body["filters"] == {"board": "renderpool"}
    assert [g["board"] for g in body["boards"]] == ["renderpool"]
    assert [n["id"] for n in body["boards"][0]["nodes"]] == ["t_z"]
    assert body["boards"][0]["edges"] == []
    assert client.get(f"{MOUNT}/graph?board=nope").json()["boards"] == []


def test_graph_endpoint_rejects_an_oversized_board_slug(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Same bound as `/watch`: the slug is a path-ish token, not free text."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    assert make_client(mod).get(f"{MOUNT}/graph?board={'x' * 65}").status_code == 422


# --- the /chats endpoint (ADR-0007) ------------------------------------------

# A session store with one of every shape the read has to sort out, next to the
# fixture boards: two human root chats, a subagent, a kanban worker, a cron tick,
# a oneshot run, an archived chat, a hidden chat, and a *human* child session.
CHAT_ROWS = [
    ("s-human", "tui", None, "Review GPU pool work", 10.0, 900.0, 0, 0, "default", 5000, 2500),
    ("s-local", "local", None, None, 11.0, 800.0, 0, 0, "researcher-a", 1200, 640),
    ("s-sub", "subagent", "s-human", "child agent", 12.0, 950.0, 0, 0, "default"),
    ("s-worker", "kanban", None, "worker session", 13.0, 990.0, 0, 0, "default"),
    ("s-cron", "cron", None, "cron tick", 14.0, 980.0, 0, 0, "default"),
    ("s-oneshot", "oneshot", None, "oneshot run", 15.0, 970.0, 0, 0, "default"),
    ("s-arch", "local", None, "archived chat", 16.0, 960.0, 1, 0, "default"),
    ("s-hidden", "local", None, "hidden chat", 17.0, 955.0, 0, 1, "default"),
    ("s-child", "local", "s-human", "spawned child chat", 18.0, 940.0, 0, 0, "default"),
    # the fixture boards' own worker sessions, so card rows can resolve tokens
    ("sess-1", "kanban", None, "worker run", 100.0, 200.0, 0, 0, "default", 210000, 105000),
    ("sess-2", "kanban", None, "worker run", 100.0, 300.0, 0, 0, "default", 120000, 64000),
    ("sess-3", "kanban", None, "worker run", 100.0, 400.0, 0, 0, "default", 205000, 137000),
]


def _fixture_store(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The fixture boards plus a fixture ``state.db`` under the same home."""
    _fixture_home(tmp_path, monkeypatch)
    _state(tmp_path, CHAT_ROWS)


def test_chats_endpoint_lists_root_chats_with_resume_links(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The third section: this host's own chats, newest activity first, each with
    a resume link built from the session's own ``profile_name``."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)

    resp = make_client(mod).get(f"{MOUNT}/chats")
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body) == ["chats", "filters", "generated_at"]
    assert isinstance(body["generated_at"], int)
    assert body["filters"] == {"sources": ["local", "tui"], "limit": 25}
    assert [
        (r["session_id"], r["title"], r["source"], r["last_activity_at"]) for r in body["chats"]
    ] == [
        ("s-human", "Review GPU pool work", "tui", 900.0),
        ("s-local", "(untitled)", "local", 800.0),
    ]
    assert body["chats"][0]["chat_link"] == "/chat?profile=default&resume=s-human"
    assert body["chats"][1]["chat_link"] == "/chat?profile=researcher-a&resume=s-local"
    assert (body["chats"][0]["tokens_in"], body["chats"][0]["tokens_out"]) == (5000, 2500)
    assert (body["chats"][1]["tokens_in"], body["chats"][1]["tokens_out"]) == (1200, 640)


# --- token usage on the card sections (ADR-0010) ------------------------------


def test_watch_rows_carry_token_counts_from_the_store(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Every card row's session resolves to its store counts."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)

    dev = make_client(mod).get(f"{MOUNT}/watch").json()["boards"][0]
    run = dev["running"][0]
    assert (run["task_id"], run["tokens_in"], run["tokens_out"]) == ("t_run", 210000, 105000)
    assert [(r["task_id"], r["tokens_in"], r["tokens_out"]) for r in dev["recent"]] == [
        ("t_done", 120000, 64000),
        ("t_review", 205000, 137000),
    ]


def test_watch_rows_answer_null_tokens_without_a_store(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """No session store: the keys stay (one payload shape) and read as unknown."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)

    dev = make_client(mod).get(f"{MOUNT}/watch").json()["boards"][0]
    for row in dev["running"] + dev["recent"]:
        assert row["tokens_in"] is None and row["tokens_out"] is None


def test_graph_nodes_carry_token_counts_and_nulls(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Nodes resolve the same way: known session -> counts, unknown or absent
    session -> None; the graph and the tables never disagree."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)

    graph = make_client(mod).get(f"{MOUNT}/graph?board=devtools").json()["boards"][0]
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert (by_id["t_run"]["tokens_in"], by_id["t_run"]["tokens_out"]) == (210000, 105000)
    # sess-0 (t_old) is in no store row: the session exists, its counts do not
    assert by_id["t_old"]["session_id"] == "sess-0"
    assert by_id["t_old"]["tokens_in"] is None
    alone = by_id["t_nosess"]
    assert alone["session_id"] is None
    assert alone["tokens_in"] is None and alone["tokens_out"] is None


def test_chats_endpoint_excludes_every_non_chat_session(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Subagents, workers, timers, archived, hidden and child sessions stay out."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)
    ids = {r["session_id"] for r in make_client(mod).get(f"{MOUNT}/chats").json()["chats"]}
    assert ids == {"s-human", "s-local"}


def test_chats_endpoint_source_param_adds_cron_and_oneshot(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The on-demand knob: repeat ``source`` to widen the source filter."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)
    client = make_client(mod)

    body = client.get(f"{MOUNT}/chats?source=cron&source=oneshot").json()
    assert body["filters"] == {"sources": ["cron", "oneshot"], "limit": 25}
    assert [(r["session_id"], r["source"]) for r in body["chats"]] == [
        ("s-cron", "cron"),
        ("s-oneshot", "oneshot"),
    ]

    # a source the store has no rows for simply answers with none, and the echo
    # says which sources were asked for
    body = client.get(f"{MOUNT}/chats?source=carrier-pigeon").json()
    assert body["chats"] == []
    assert body["filters"]["sources"] == ["carrier-pigeon"]


def test_chats_endpoint_limit_caps_and_is_bounded(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One row for ``?limit=1``; out-of-range is 422, the same bound as `/watch`."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)
    client = make_client(mod)

    body = client.get(f"{MOUNT}/chats?limit=1").json()
    assert [r["session_id"] for r in body["chats"]] == ["s-human"]
    assert body["filters"]["limit"] == 1
    assert client.get(f"{MOUNT}/chats?limit=0").status_code == 422
    assert client.get(f"{MOUNT}/chats?limit=101").status_code == 422


def test_chats_endpoint_rejects_an_empty_or_oversized_source(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """An empty ``source=`` would read as "no chats" rather than as a typo."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)
    client = make_client(mod)

    resp = client.get(f"{MOUNT}/chats?source=")
    assert resp.status_code == 422
    assert "source" in resp.text
    assert client.get(f"{MOUNT}/chats?source={'x' * 33}").status_code == 422


def test_chats_endpoint_without_a_state_db_is_an_empty_section(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A host with no session store still gets the section, empty, not an error."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    assert (tmp_path / "state.db").is_file() is False

    resp = make_client(mod).get(f"{MOUNT}/chats")
    assert resp.status_code == 200
    assert resp.json()["chats"] == []


def test_chats_endpoint_ignores_a_corrupt_store(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The boards still answer; a store this code cannot read is an empty section."""
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    (tmp_path / "state.db").write_bytes(b"not a database")

    body = make_client(mod).get(f"{MOUNT}/chats").json()
    assert body["chats"] == []


def test_chats_endpoint_survives_a_time_column_holding_a_blob(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One unreadable time must not take the section down.

    SQLite gives a BLOB no column affinity, so `last_activity_at` can arrive as
    bytes; bytes in the payload fail the response encoder outside `sqlite3.Error`,
    which is a 500 for every chat on the host. The endpoint answers, the row is
    there, and its time reads as unknown.
    """
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    _state(tmp_path, CHAT_ROWS)
    con = sqlite3.connect(tmp_path / "state.db")
    con.execute(
        "insert into sessions (id, source, title, started_at, last_activity_at)"
        " values ('s_blob', 'local', 'blob time', 10, ?)",
        (b"\xff\xfe",),
    )
    con.commit()
    con.close()

    resp = make_client(mod).get(f"{MOUNT}/chats")
    assert resp.status_code == 200
    blob = [row for row in resp.json()["chats"] if row["session_id"] == "s_blob"]
    assert blob and blob[0]["last_activity_at"] is None


def test_chats_endpoint_boards_are_not_filtered_by_the_chat_source(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The two reads stay independent: `/chats` has no board param and `/watch`
    has no source param, and each ignores the other's shape."""
    mod = load_plugin_api(monkeypatch)
    _fixture_store(tmp_path, monkeypatch)
    client = make_client(mod)

    assert "boards" not in client.get(f"{MOUNT}/chats").json()
    assert "chats" not in client.get(f"{MOUNT}/watch").json()


def test_the_chats_harness_pins_bite(tmp_path) -> None:
    """Negative controls for the root-chats half: the row key, the row count the
    card carries, the resume anchor, the words in the cells, the defensive read of
    a payload of the wrong shape, and the card tables' own row key. Each must
    redden the harness or move the pinned facts."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    pinned = _run_node_harness(tmp_path)
    mutations = {
        # the chats table is keyed by a field a chat row does not have
        "wrong_key_field.js": bundle.replace('keyField: "session_id"', 'keyField: "task_id"'),
        # the shared default moves: the card tables lose their keys
        "wrong_default_key.js": bundle.replace(
            'var keyField = props.keyField || "task_id";',
            'var keyField = props.keyField || "session_id";',
        ),
        # the card's row count stops matching the rows it renders
        "wrong_count.js": bundle.replace(
            'h(Card, { "data-chats": chats.length }', 'h(Card, { "data-chats": 0 }'
        ),
        # the resume column renders as text: the anchors are gone
        "no_chat_anchor.js": bundle.replace(
            '["chat_link", "Resume", "link", "22%"],', '["chat_link", "Resume", "text", "22%"],'
        ),
        # a column of the contract stops reaching the page
        "no_source_cell.js": bundle.replace(
            '["source", "Source", "text", "12%"],', '["source", "Source", "tree", "12%"],'
        ),
        # the time cell prints the raw value instead of the relative time
        "raw_time.js": bundle.replace("value ? timeAgo(value) : ", "value ? String(value) : "),
        # the bundle builds the resume URL itself instead of reading chat_link
        "composed_href.js": bundle.replace(
            'h("a", { href: r.chat_link, className: "underline" }',
            'h("a", { href: "/chat?profile=" + r.profile + "&resume=" + r.session_id,'
            ' className: "underline" }',
        ),
        # a payload that has not arrived yet throws inside the render
        "unguarded_chats_of.js": bundle.replace(
            "return Array.isArray(rows) ? rows : [];", "return rows || [];"
        ),
    }
    for name, mutated in mutations.items():
        assert mutated != bundle, f"{name} no longer finds its victim line"
        target = tmp_path / name
        target.write_text(mutated, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(target)],
            capture_output=True,
            text=True,
        )
        reddened = proc.returncode != 0
        moved = proc.stdout.strip() != pinned
        assert reddened or moved, f"{name} left the harness green and the facts unchanged"


def test_the_grid_and_status_harness_pins_bite(tmp_path) -> None:
    """Negative controls for the table half: the fixed grid dropped, the grid
    widths neutralised to auto, the status cell rendered as plain text without
    its dot, the running section's shared Status column removed, and each
    remapped palette colour reverted to the old graph-only value (ADR-0009);
    then the Tokens column dropped from every card table, dropped from only the
    Recent section (the shared grid splits), dropped from chats, stripped of its
    cell marker, and the counts left raw without the compact format
    (ADR-0010); and the empty-status placeholder branch removed, so that row
    grows a fallback dot it never had. Each must redden the harness or move the
    facts pinned above."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    pinned = _run_node_harness(tmp_path)
    mutations = {
        # tables fall back to content-driven widths: columns stop aligning
        "no_fixed_grid.js": bundle.replace(
            'h("table", { className: "w-full text-sm table-fixed" },',
            'h("table", { className: "w-full text-sm" },',
        ),
        # the colgroup survives but every width reads auto: alignment is gone
        "auto_widths.js": bundle.replace(
            'style: { width: c[3] || "auto" }', 'style: { width: "auto" }'
        ),
        # status cells lose the palette dot and render as bare text
        "status_as_text.js": bundle.replace(
            '["status", "Status", "status", "10%"],', '["status", "Status", "text", "10%"],'
        ),
        # the running section drops the shared Status column: the grids split
        "running_no_status_col.js": bundle.replace(
            '    ["status", "Status", "status", "10%"],\n'
            '    ["assignee", "Assignee", "text", "10%"],\n'
            '    ["tokens", "Tokens in/out", "tokens", "18%"],\n'
            '    ["run_started_at", "Running for", "ago", "14%"],',
            '    ["assignee", "Assignee", "text", "10%"],\n'
            '    ["tokens", "Tokens in/out", "tokens", "18%"],\n'
            '    ["run_started_at", "Running for", "ago", "14%"],',
        ),
        # running back to the old graph amber: cell and dot disagree again
        "old_running_fill.js": bundle.replace('running: "#3b82f6",', 'running: "#f59e0b",'),
        # review back to the old graph sky
        "old_review_fill.js": bundle.replace('review: "#facc15",', 'review: "#38bdf8",'),
        # the empty-status placeholder branch removed: that row gets a lone
        # fallback dot claiming a status it does not have
        "always_dot_status.js": bundle.replace(
            '      if (label === "") {\n'
            "        return h(\n"
            '          "span",\n'
            '          { className: "text-muted-foreground", "data-placeholder": "status" },\n'
            '          "\\u2014"\n'
            "        );\n"
            "      }\n",
            "",
        ),
        # the Tokens column is dropped from every table (ADR-0010): the cell
        # and tokens guards count rows against it and reddened
        "no_tokens_col.js": bundle.replace(
            '    ["tokens", "Tokens in/out", "tokens", "18%"],\n', ""
        ),
        # only the Recent section drops it: the two shared grids stop matching
        "recent_no_tokens_col.js": bundle.replace(
            '["tokens", "Tokens in/out", "tokens", "18%"],\n    ["session_at"', '["session_at"'
        ),
        # only the chats table drops it: its five-column contract breaks
        "chats_no_tokens.js": bundle.replace(
            '["tokens", "Tokens in/out", "tokens", "16%"],\n    ["last_activity_at"',
            '["last_activity_at"',
        ),
        # the tokens cells go unmarked: the harness cannot see them at all
        "unmarked_tokens.js": bundle.replace('"data-tokens": "none"', '"data-x": "none"').replace(
            '"data-tokens": shown', '"data-x": shown'
        ),
        # the counts reach the page raw: columns stop being comparable at a
        # glance and the pinned formatted strings move
        "raw_token_numbers.js": bundle.replace(
            'if (n >= 10000) return Math.round(n / 1000) + "k";', ""
        ).replace('if (n >= 1000) return parseFloat((n / 1000).toFixed(1)) + "k";', ""),
    }
    for name, mutated in mutations.items():
        assert mutated != bundle, f"{name} no longer finds its victim line"
        target = tmp_path / name
        target.write_text(mutated, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(target)],
            capture_output=True,
            text=True,
        )
        reddened = proc.returncode != 0
        moved = proc.stdout.strip() != pinned
        assert reddened or moved, f"{name} left the harness green and the facts unchanged"


# --- the self-contained install shape (ADR-0018) ------------------------------

# A plugin-catalog install copies the plugin subdir alone (manifest, bundle,
# plugin_api.py and the package beside it) and loads plugin_api.py from the
# copy. This probe runs that shape in a fresh interpreter: cwd and sys.path hold
# no devtools repo, the venv carries no top-level workerwatch package, and the
# routes must still answer off a fixture board inside the copy's own home.
_CATALOG_PROBE = '''
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path

copy_root = Path(sys.argv[1]).resolve()
home = copy_root.parent / "hermes-home"
board = home / "kanban" / "boards" / "solo"
board.mkdir(parents=True)
con = sqlite3.connect(board / "kanban.db")
con.executescript("""
CREATE TABLE tasks (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    body               TEXT,
    assignee           TEXT,
    status             TEXT NOT NULL,
    current_run_id     INTEGER,
    started_at         INTEGER,
    claim_lock         TEXT,
    block_kind         TEXT,
    last_failure_error TEXT,
    result             TEXT,
    completed_at       INTEGER
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, profile TEXT, started_at INTEGER
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT, payload TEXT,
    created_at INTEGER
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, author TEXT, body TEXT,
    created_at INTEGER
);
""")
con.execute(
    "insert into tasks (id, title, assignee, status) values ('t_1', 'one', 'default', "
    "'running')"
)
con.commit()
con.close()
os.environ["HERMES_HOME"] = str(home)

try:
    import workerwatch  # noqa: F401 - the point of the probe: this must NOT resolve
    print("PROBE-FAIL workerwatch resolves before the plugin is loaded")
    raise SystemExit(1)
except ImportError:
    pass

spec = importlib.util.spec_from_file_location(
    "catalog_copy_api", copy_root / "dashboard" / "plugin_api.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import workerwatch.worker_watch as ww

if Path(ww.__file__).resolve().parent.parent != copy_root:
    print(f"PROBE-FAIL worker_watch loaded from {ww.__file__}, not the copy")
    raise SystemExit(1)

from fastapi import FastAPI
from fastapi.testclient import TestClient

app = FastAPI()
app.include_router(mod.router, prefix="/api/plugins/worker-watch")
client = TestClient(app)
paths = ("/watch", "/graph", "/chats", "/card?board=solo&task=t_1", "/stalled")
got = {p: client.get("/api/plugins/worker-watch" + p).status_code for p in paths}
print("statuses", got)
bad = {p: c for p, c in got.items() if c != 200}
if bad:
    print("PROBE-FAIL", bad)
    raise SystemExit(1)
snapshot = client.get("/api/plugins/worker-watch/watch").json()
if not any(b.get("board") == "solo" for b in snapshot.get("boards", [])):
    print("PROBE-FAIL the fixture board is missing from the snapshot")
    raise SystemExit(1)
print("CATALOG-OK")
'''


def test_a_catalog_copy_mounts_without_the_repo(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The copied subdir is the whole install: routes mount, reads run, no repo.

    This is the card's E2E entry point — a plugin-catalog install clones the
    entry repo, checks out the pinned SHA and copies only that subdir under
    ~/.hermes/plugins. Everything the plugin reads must ride inside the copy.
    """
    dest = tmp_path / "worker-watch"
    shutil.copytree(PLUGIN.parent, dest, ignore=shutil.ignore_patterns("__pycache__"))
    probe = tmp_path / "catalog_probe.py"
    probe.write_text(_CATALOG_PROBE, encoding="utf-8")
    r = subprocess.run(
        ["uv", "run", "--project", str(REPO), "--quiet", "python", str(probe), str(dest)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=180,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CATALOG-OK" in r.stdout, r.stdout + r.stderr


def test_a_copy_without_its_package_fails_the_import_loudly(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A dashboard copied without its sibling package must raise at import.

    A half-copied install may not mount a router whose every route raises at
    request time — the dashboard log has to show the missing package.
    """
    broken = tmp_path / "broken"
    shutil.copytree(PLUGIN, broken / "dashboard", ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(RuntimeError, match="workerwatch"):
        _load_from(broken / "dashboard" / "plugin_api.py")


def test_a_copy_missing_one_read_module_fails_the_import_loudly(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The store read is its own module, so it keeps its own import guard.

    A copy that has the board read but not the store read must not mount a
    plugin whose `/chats` route raises at request time — the mount would look
    healthy and the section would break on first poll.
    """
    dest = tmp_path / "worker-watch"
    shutil.copytree(PLUGIN.parent, dest, ignore=shutil.ignore_patterns("__pycache__"))
    (dest / "workerwatch" / "root_chats.py").unlink()
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with pytest.raises(RuntimeError, match="root_chats.py"):
        _load_from(dest / "dashboard" / "plugin_api.py")


def test_the_deployed_symlink_mounts_with_no_repo_resolution(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Deployment stays a symlink under ~/.hermes/plugins: loading plugin_api.py
    through the link must mount the routes — no DEVTOOLS_REPO, no parent-count
    guessing (ADR-0018 replaces ADR-0002's repo resolution)."""
    link_dir = tmp_path / "worker-watch-dashboard"
    link_dir.symlink_to(PLUGIN, target_is_directory=True)
    monkeypatch.delenv("DEVTOOLS_REPO", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    mod = _load_from(link_dir / "plugin_api.py")
    routes = {route.path for route in mod.router.routes}
    assert {"/watch", "/graph", "/chats", "/card", "/stalled", "/restart"} <= routes


def test_bundle_is_syntactically_valid_javascript(tmp_path) -> None:
    """No JS test harness here, so guard the bundle with node --check (skip when
    node is absent). The negative control proves the check bites."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    assert subprocess.run([node, "--check", str(PLUGIN / "dist" / "index.js")]).returncode == 0
    broken = tmp_path / "broken.js"
    broken.write_text(bundle + "\n)))garbage((\n", encoding="utf-8")
    assert subprocess.run([node, "--check", str(broken)]).returncode != 0


# --- the /card endpoint (ADR-0012) -------------------------------------------


def test_card_endpoint_answers_the_drawer(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    # the narrow fixture board has no session_id column (an old slice): the
    # drawer answers its facts and simply has no session half
    resp = make_client(mod).get(f"{MOUNT}/card", params={"board": "devtools", "task": "t_done"})
    assert resp.status_code == 200
    card = resp.json()
    assert isinstance(card["generated_at"], int)
    assert (card["board"], card["id"], card["title"]) == ("devtools", "t_done", "finished card")
    assert card["status"] == "done"
    assert [e["kind"] for e in card["events"]] == ["owner_admitted"]
    assert card["comments"] == []
    assert card["session"] is None


def test_card_endpoint_joins_the_admitted_session(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from tests.test_card_detail import _board as _full_board
    from tests.test_card_detail import _store as _store_full

    mod = load_plugin_api(monkeypatch)
    _full_board(
        tmp_path,
        "detail",
        tasks=[
            (
                "t_s",
                "session card",
                "do the thing",
                "default",
                "done",
                None,
                None,
                "shipped",
                1,
                2,
                3,
                None,
                None,
                None,
            )
        ],
        events=[("t_s", "owner_admitted", '{"session_id": "s_w"}', 3)],
        comments=[("t_s", "dan", "nice", 4)],
    )
    _store_full(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    resp = make_client(mod).get(f"{MOUNT}/card", params={"board": "detail", "task": "t_s"})
    assert resp.status_code == 200
    card = resp.json()
    assert card["body"] == "do the thing"
    assert [c["body"] for c in card["comments"]] == ["nice"]
    assert card["session"]["model"] == "qwen-max"
    assert card["session"]["tokens_in"] == 215800
    assert card["session"]["tokens_cache_read"] == 1200000
    assert card["session"]["chat_link"] == "/chat?profile=default&resume=s_w"


def test_card_endpoint_404s_anything_it_cannot_answer(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _fixture_home(tmp_path, monkeypatch)
    client = make_client(mod)
    assert (
        client.get(f"{MOUNT}/card", params={"board": "nosuch", "task": "t_done"}).status_code == 404
    )
    assert (
        client.get(f"{MOUNT}/card", params={"board": "devtools", "task": "t_nope"}).status_code
        == 404
    )
    assert (
        client.get(f"{MOUNT}/card", params={"board": "broken", "task": "t_done"}).status_code == 404
    )
    # both params are required, and the slug bound matches the other routes
    assert client.get(f"{MOUNT}/card", params={"board": "devtools"}).status_code == 422
    assert (
        client.get(f"{MOUNT}/card", params={"board": "x" * 65, "task": "t_done"}).status_code == 422
    )


# --- the parked-card read and the one write (ADR-0013) -----------------------


def _parked_home(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A board root whose only interesting card is parked: ready + assigned + a
    quota stamp. Built with the feat-0013 fixture schema (it carries the failure
    columns the read-side fixture in test_worker_watch does not)."""
    from tests.test_card_restart import QUOTA
    from tests.test_card_restart import _board as parked_board

    parked_board(
        tmp_path,
        "lora",
        tasks=[("t_parked", "parked card", "default", "ready", 1, None, None, QUOTA)],
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def test_stalled_endpoint_lists_the_parked_cards(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    _parked_home(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "retry_available", lambda: True)

    body = make_client(mod).get(f"{MOUNT}/stalled").json()
    assert body["count"] == 1
    # the capability probe rides the payload (card t_1e9354dd): the bundle
    # disarms every Restart button from this one key, no second request
    assert body["retry_available"] is True
    assert [(b["board"], [r["id"] for r in b["rows"]]) for b in body["boards"]] == [
        ("lora", ["t_parked"])
    ]
    row = body["boards"][0]["rows"][0]
    assert (row["assignee"], row["status"]) == ("default", "ready")
    assert "rate-limited" in row["error"]
    # the board filter narrows it, and a board with nothing parked answers empty
    assert make_client(mod).get(f"{MOUNT}/stalled", params={"board": "lora"}).json()["count"] == 1
    assert make_client(mod).get(f"{MOUNT}/stalled", params={"board": "nope"}).json()["count"] == 0


def test_stalled_payload_carries_the_probe_as_false(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A stock install (no `kanban retry` verb) reads `retry_available: false` —
    the disarm signal, not a missing key the bundle would have to guess about."""
    mod = load_plugin_api(monkeypatch)
    _parked_home(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "retry_available", lambda: False)
    assert make_client(mod).get(f"{MOUNT}/stalled").json()["retry_available"] is False


def test_restart_maps_a_success(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    seen = {}

    def fake_restart(board, task_id):
        seen["args"] = (board, task_id)
        return {
            "ok": True,
            "board": board,
            "task_id": task_id,
            "status": "ready",
            "cleared_error": "stamp",
            "steps": ["retry"],
        }

    mod.restart_card = fake_restart
    body = (
        make_client(mod)
        .post(f"{MOUNT}/restart", json={"board": "lora", "task_id": "t_parked"})
        .json()
    )
    assert seen["args"] == ("lora", "t_parked")
    assert body["ok"] is True and body["steps"] == ["retry"]
    assert isinstance(body["generated_at"], int)


def test_restart_maps_each_refusal_to_its_http_class(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    client = make_client(mod)
    cases = [
        ({"ok": False, "status": 404, "reason": "no card"}, 404),
        (
            {
                "ok": False,
                "status": 409,
                "reason": "a run is active on this card: reclaim it first",
            },
            409,
        ),
        ({"ok": False, "status": 502, "reason": "unblock failed (rc=1): nope"}, 502),
    ]
    for payload, expected in cases:
        mod.restart_card = lambda board, task_id, _p=payload: dict(_p)  # type: ignore[misc]
        resp = client.post(f"{MOUNT}/restart", json={"board": "lora", "task_id": "t_parked"})
        assert resp.status_code == expected
        assert resp.json()["detail"] == payload["reason"]


def test_restart_validates_its_body_and_is_the_only_write(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    mod = load_plugin_api(monkeypatch)
    client = make_client(mod)
    assert client.post(f"{MOUNT}/restart", json={}).status_code == 422
    assert client.post(f"{MOUNT}/restart", json={"board": "lora"}).status_code == 422
    assert (
        client.post(f"{MOUNT}/restart", json={"board": "x" * 65, "task_id": "t"}).status_code == 422
    )
    # the rest of the router stays GET-only, including the new read
    assert client.get(f"{MOUNT}/restart").status_code == 405
    for verb in ("post", "put", "patch", "delete"):
        assert getattr(client, verb)(f"{MOUNT}/stalled").status_code == 405


def test_the_stalled_disarm_pins_bite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Negative controls for the Restart disarm (ADR-0016): a button that
    ignores the probe flag, a disarmed row that loses its marked placeholder,
    and a capability helper that GUESSES when the key is missing. Each must
    redden the harness or move its facts, or the disarm asserts nothing."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    pinned = _run_node_harness(tmp_path)
    mutations = {
        # the button stops consulting the flag at all: every parked row offers
        # a Restart, probe or not (the off-fixture still renders, so this is
        # the mutation that proves the RENDERER's guard, not just the helper's)
        "retry_ignores_the_flag.js": bundle.replace("props.retryAvailable === true", "true"),
        # the disarmed cell loses its marked placeholder: rows read as broken
        # tables instead of a named absence
        "disarm_unmarked.js": bundle.replace(
            ': h("span", { className: "text-xs text-muted-foreground",\n'
            '                                "data-restart-off": key }, "unavailable"),',
            ": null,",
        ),
        # a payload without the probe key becomes AVAILABLE: capability guessed
        # from absence, the exact thing ADR-0016 forbids
        "retry_on_absence.js": bundle.replace(
            "payload.retry_available === true", "payload.retry_available !== false"
        ),
    }
    for name, mutated in mutations.items():
        assert mutated != bundle, f"mutation {name} did not apply"
        victim = tmp_path / name
        victim.write_text(mutated, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(victim)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0 or proc.stdout.strip() != pinned, f"{name} went unnoticed"


def test_the_pinned_strip_pins_bite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Negative controls for the pinned-chats strip (ADR-0017): a strip that
    never hides, a filter that pins every row, a row that loses its resume
    anchor, and a dropped star. Each must redden the harness or move its facts,
    or the strip asserts nothing about the owner's stars."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    bundle = (PLUGIN / "dist" / "index.js").read_text(encoding="utf-8")
    harness = HERE / "js" / "watch_bundle_harness.mjs"
    pinned = _run_node_harness(tmp_path)
    mutations = {
        # the filter stops reading the flag: every chat joins the strip
        "pinned_of_everything.js": bundle.replace(
            "return !!c && c.pinned === true;", "return !!c;"
        ),
        # the strip never vanishes: no pins leaves an empty frame at the top
        "strip_always_rendered.js": bundle.replace(
            "if (!Array.isArray(chats) || chats.length === 0) return null;", ";"
        ),
        # the row stops being the resume anchor: the strip's whole point
        "anchor_dropped.js": bundle.replace("href: c.chat_link,", 'href: "#",'),
        # the star glyph disappears: rows lose the mark that says why they're here
        "star_dropped.js": bundle.replace(
            '"aria-hidden": "true", className: "text-amber-400" }, "★")',
            '"aria-hidden": "true" }, "")',
        ),
    }
    for name, mutated in mutations.items():
        assert mutated != bundle, f"mutation {name} did not apply"
        victim = tmp_path / name
        victim.write_text(mutated, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(victim)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0 or proc.stdout.strip() != pinned, f"{name} went unnoticed"
