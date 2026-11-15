// Render harness for the Worker Watch bundle (ADR-0005 bundle pin, extended by
// ADR-0006 for the relationship graph, ADR-0007 for the root-chats section, and
// ADR-0008 for the back-edge colouring and the click-to-trace edge selection.
//
// The dashboard host is the only real React runtime, and there is no JS test
// harness in this repo, so this file stubs the plugin SDK, runs the IIFE in a
// sandbox, and renders one board group from a fake snapshot, one board graph, and
// the root-chats table. What it asserts is the acceptance contract: every row
// keeps a same-origin resume anchor, a 2-level chain renders depth 0/1/2, a
// session-less row keeps its placeholder, the filter bar's query string carries
// board/assignee/status, the SVG places each node at its layer with edges drawn
// as paths and unlinked cards kept out of the tree, the chats table keys its
// rows by session id with a marked placeholder for a row that cannot resume and
// puts every contract column's words on the page, the card tables keep their
// task-id keys, and a payload of the wrong shape reads as no rows. The graph
// half of ADR-0008 is pinned too: every drawn edge is a marked path with its own
// invisible hit path carrying a click handler, an edge whose child sits in the
// parent's own layer or above is classified and stroked differently from a
// forward edge, the legend count agrees with the drawn edges, selecting one edge
// thickens it, dims every other edge and marks its two endpoint nodes, a stale
// selection (an edge the payload no longer carries) reads as no selection at all,
// and the click-toggle selects, clears, and replaces correctly. The table half
// of ADR-0009 is pinned too: both card sections render one fixed column grid
// with identical widths, the chats table keeps its own fixed grid, every status
// cell carries a hex-coloured palette dot beside its word, and the cell count
// matches rows x shared columns.
//
// It prints machine-readable facts for tests/test_worker_watch_plugin.py and
// exits nonzero when the bundle exposes none of that surface, when two cards of
// one layer land on the same row, when a node with a session has no click target,
// when a board-supplied status gets no colour, when a table row is keyed by a
// field its rows do not have, when a chats row loses its anchor, its key, its
// count or a cell, or when any of the edge facts above disagree with the picture.

import { readFileSync } from "node:fs";
import { createContext, runInContext } from "node:vm";

const bundlePath = process.argv[2];
if (!bundlePath) {
  console.error("usage: watch_bundle_harness.mjs <path to dist/index.js>");
  process.exit(2);
}
const code = readFileSync(bundlePath, "utf8");

// --- stub SDK ---------------------------------------------------------------
// createElement becomes a plain node record so the tree can be walked.
function el(type, props, ...kids) {
  const flat = kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false);
  return { type, props: props || {}, kids: flat };
}
// Flatten a rendered subtree to the text a user would read. The chats section
// shares its cells with the card tables, so the words in them are only pinned if
// the walk captures them.
function textOf(node) {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (node && Array.isArray(node.kids)) return node.kids.map(textOf).join("");
  return "";
}

const stubComponent = function (props, ...kids) {
  return el("stub", props, ...kids);
};
const constHook = (init) => [init, () => {}];

const windowStub = {
  __HERMES_PLUGIN_SDK__: {
    React: { createElement: el },
    hooks: {
      useState: constHook,
      useEffect: () => {},
      useCallback: (fn) => fn,
      useMemo: (fn) => fn(),
      useRef: () => ({ current: null }),
    },
    components: new Proxy({}, { get: () => stubComponent }),
    fetchJSON: () => new Promise(() => {}),
    utils: { timeAgo: (n) => "ago" + n, cn: (...parts) => parts.join(" ") },
  },
  __HERMES_PLUGINS__: { register: () => {} },
};

const context = createContext();
Object.assign(context, { window: windowStub, URLSearchParams, console });
runInContext(code, context, { filename: bundlePath });

const hooks = windowStub.__HERMES_WORKER_WATCH__;
if (
  !hooks ||
  !hooks.BoardGroup ||
  !hooks.watchUrl ||
  !hooks.BoardGraph ||
  !hooks.graphLayout ||
  !hooks.graphUrl ||
  !hooks.RootChats ||
  !hooks.chatsUrl ||
  !hooks.chatsOf ||
  !hooks.PinnedStrip ||
  !hooks.pinnedOf ||
  !hooks.sameEdge ||
  !hooks.nextEdge ||
  !hooks.sameNode ||
  !hooks.nextNode ||
  !hooks.fmtTokens ||
  !hooks.fmtExact ||
  !hooks.tokensTip ||
  !hooks.cardUrl ||
  !hooks.findNode ||
  !hooks.DetailPanel ||
  !hooks.stalledRetryOn
) {
  console.error("bundle exports no pin surface (window.__HERMES_WORKER_WATCH__)");
  process.exit(2);
}

// --- one board group: a running child plus a 2-level chain of history -------
// `tokens` is the session's [input, output] counts from the store (ADR-0010);
// a row with no session has none. The Tokens cell formats them, and the cell
// for a session-less row must read as the marked placeholder, not a blank.
function row(taskId, parentId, depth, status, sessionId, tokens) {
  return {
    board: "devtools",
    task_id: taskId,
    parent_id: parentId,
    depth: depth,
    title: taskId + " title",
    assignee: "default",
    status: status,
    profile: "default",
    run_started_at: 1000,
    session_id: sessionId,
    session_at: sessionId ? 2000 : null,
    chat_link: sessionId ? "/chat?profile=default&resume=" + sessionId : null,
    tokens_in: tokens ? tokens[0] : null,
    tokens_out: tokens ? tokens[1] : null,
    tokens_cache_read: tokens && tokens.length > 2 ? tokens[2] : null,
    tokens_cache_write: null,
    tokens_reasoning: null,
  };
}

const group = {
  board: "devtools",
  running: [row("t_run", "t_root", 1, "running", null)],
  recent: [
    row("t_root", null, 0, "done", "s0", [5000, 250]),
    row("t_mid", "t_root", 1, "done", "s1", [215800, 113296, 1200000]),
    row("t_leaf", "t_mid", 2, "review", "s2", [1200, 640]),
    // the blocked palette member, with no session: its coloured status dot is
    // pinned next to its marked placeholder, so neither can be keyed off the
    // other
    row("t_blocked", null, 0, "blocked", null),
    // a card whose status column is empty: its status cell must read as the
    // marked placeholder alone — no lone fallback-coloured dot
    row("t_nostatus", null, 0, null, null),
  ],
};

const tree = hooks.BoardGroup({ group: group });

// --- walk the element tree -------------------------------------------------
const anchors = [];
const depths = [];
const boards = [];
const cardRowKeys = [];
let placeholders = 0;
(function walk(node) {
  if (Array.isArray(node)) {
    node.forEach(walk);
    return;
  }
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (node.type === "a" && props.href) anchors.push(String(props.href));
  if (node.type === "tr" && props.key !== undefined) cardRowKeys.push(String(props.key));
  if (Object.prototype.hasOwnProperty.call(props, "data-depth")) {
    depths.push(String(props["data-depth"]));
  }
  if (props["data-group"]) boards.push(String(props["data-group"]));
  // the session-less cell is marked, so a bundle that drops it is visible here
  if (props["data-placeholder"] === "chat") placeholders += 1;
  if (Array.isArray(node.kids)) node.kids.forEach(walk);
})(tree);

// The card tables are keyed by task id — the shared SectionTable's default. A
// default that flips to another field leaves every card row unkeyed, and a
// refresh that reorders rows then reuses the wrong one.
const cardKeyFacts = [];
if (cardRowKeys.length === 0) cardKeyFacts.push("no-rows");
for (const key of cardRowKeys) {
  if (key === "undefined" || key === "null" || key === "") cardKeyFacts.push("blank-key");
}
for (const row of group.running.concat(group.recent)) {
  if (cardRowKeys.indexOf(row.task_id) < 0) cardKeyFacts.push("missing:" + row.task_id);
}
cardKeyFacts.sort();

const query = hooks.watchUrl({
  board: "devtools",
  assignee: "reviewer",
  statuses: ["running", "done"],
});

// --- one board graph: a 2-level chain plus a card in no link ----------------
// nodes carry the store-joined token counts (ADR-0010) so the summary panel
// has counts to show next to the resume link (ADR-0011).
const NODE_TOKENS = { s0: [5000, 250], s1: [1200, 640], s2: [215800, 113296], s9: [900, 50] };
function graphNode(taskId, depth, status, sessionId) {
  const tok = sessionId ? NODE_TOKENS[sessionId] : null;
  return {
    id: taskId,
    title: taskId + " title",
    status: status,
    depth: depth,
    session_id: sessionId,
    profile: "default",
    chat_link: sessionId ? "/chat?profile=default&resume=" + sessionId : null,
    tokens_in: tok ? tok[0] : null,
    tokens_out: tok ? tok[1] : null,
  };
}

const graph = {
  board: "devtools",
  nodes: [
    graphNode("t_root", 0, "done", "s0"),
    graphNode("t_c1", 1, "done", "s1"),
    graphNode("t_c2", 1, "review", null),
    graphNode("t_gchild", 2, "running", "s2"),
    // a second parent sitting in the card's own layer, with a status outside the
    // colour table. Its id sorts before the shallow parent's, so a layout that
    // hangs each card on the first parent in id order picks this one and stacks
    // the two cards on one row; the nearest-parent rule picks t_c2 instead.
    graphNode("t_a2", 2, "constructor", null),
    graphNode("t_alone", 0, "todo", "s9"),
  ],
  edges: [
    { parent: "t_root", child: "t_c1" },
    { parent: "t_root", child: "t_c2" },
    { parent: "t_c2", child: "t_gchild" },
    { parent: "t_a2", child: "t_gchild" },
    // a link that runs back toward a shallower layer: t_c1 sits in layer 1 and
    // its new parent in layer 2, so this curve is drawn right-to-left. It forms
    // no cycle, and t_c1's nearest parent stays t_root, so no tree edge moves.
    { parent: "t_gchild", child: "t_c1" },
  ],
};

const layout = hooks.graphLayout(graph);
const graphTree = hooks.BoardGraph({
  graph: graph,
  selected: null,
  onSelect: function () {},
  selectedNode: null,
  onSelectNode: function () {},
});

function firstCircleFill(node) {
  if (node && node.type === "circle") return String(node.props.fill);
  const kids = node && Array.isArray(node.kids) ? node.kids : [];
  for (const kid of kids) {
    const found = firstCircleFill(kid);
    if (found) return found;
  }
  return "";
}

function firstChipColour(node) {
  if (node && node.props && node.props.style && node.props.style.background) {
    return String(node.props.style.background);
  }
  const kids = node && Array.isArray(node.kids) ? node.kids : [];
  for (const kid of kids) {
    const found = firstChipColour(kid);
    if (found) return found;
  }
  return "";
}

let svgs = 0;
let graphPaths = 0;
let graphDots = 0;
let graphBoard = "";
const graphAnchors = [];
const placedNodes = [];
const unlinkedShown = [];
const fills = [];
const chipFills = [];
const edgeDirs = [];
const edgeStrokes = [];
let hitPaths = 0;
let hitClicks = 0;
let backLegend = -1;
let nodeClicks = 0;
let chipClicks = 0;
(function walkGraph(node) {
  if (Array.isArray(node)) {
    node.forEach(walkGraph);
    return;
  }
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (node.type === "svg") svgs += 1;
  if (node.type === "path") graphPaths += 1;
  if (node.type === "circle") graphDots += 1;
  if (node.type === "a" && props.href) graphAnchors.push(String(props.href));
  if (props["data-graph"]) graphBoard = String(props["data-graph"]);
  if (node.type === "path" && props["data-edge"]) {
    const id = String(props["data-edge"]);
    edgeDirs.push(id + ":" + String(props["data-edge-dir"]));
    edgeStrokes.push(id + ":" + String(props.stroke));
  }
  if (props["data-edge-hit"]) {
    hitPaths += 1;
    if (typeof props.onClick === "function") hitClicks += 1;
  }
  if (props["data-back-edges"] !== undefined) backLegend = Number(props["data-back-edges"]);
  if (props["data-node"]) {
    const id = String(props["data-node"]);
    if (typeof props.onClick === "function") nodeClicks += 1;
    placedNodes.push({
      id: id,
      depth: Number(props["data-depth"]),
      x: Number(props["data-x"]),
      y: Number(props["data-y"]),
    });
    fills.push(id + ":" + firstCircleFill(node));
  }
  if (props["data-unlinked"]) {
    const id = String(props["data-unlinked"]);
    if (typeof props.onClick === "function") chipClicks += 1;
    unlinkedShown.push(id);
    chipFills.push(id + ":" + firstChipColour(node));
  }
  if (Array.isArray(node.kids)) node.kids.forEach(walkGraph);
})(graphTree);

// --- edge selection (ADR-0008): one selected edge per tab, clicked to trace --
// The tab owns the selection state and BoardGraph renders what it is handed, so
// the harness can pin the selected picture directly: the chosen edge drawn
// thick and full-opacity, every other edge dimmed, both endpoints marked.
function selWalk(node, acc, selId) {
  if (Array.isArray(node)) {
    node.forEach(function (k) {
      selWalk(k, acc, selId);
    });
    return;
  }
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (node.type === "path" && props["data-edge"]) {
    if (String(props["data-edge"]) === selId) {
      acc.selStyle = String(props.strokeWidth) + "@" + String(props.strokeOpacity);
    }
    if (Number(props.strokeOpacity) === 0.12) acc.dim += 1;
  }
  if (props["data-edge-node"]) acc.nodes += 1;
  if (Array.isArray(node.kids)) node.kids.forEach(function (k) { selWalk(k, acc, selId); });
}

const selAcc = { selStyle: "", dim: 0, nodes: 0 };
selWalk(
  hooks.BoardGraph({
    graph: graph,
    selected: { board: "devtools", parent: "t_a2", child: "t_gchild" },
    onSelect: function () {},
  }),
  selAcc,
  "t_a2>t_gchild"
);

// a selection the rescan no longer carries (the card or its link is gone) must
// read as no selection at all: it may not dim the board with nothing to point at
const ghostAcc = { selStyle: "", dim: 0, nodes: 0 };
selWalk(
  hooks.BoardGraph({
    graph: graph,
    selected: { board: "devtools", parent: "t_root", child: "t_nowhere" },
    onSelect: function () {},
  }),
  ghostAcc,
  "t_root>t_nowhere"
);

// the click-toggle: first click selects, clicking the same edge clears, another
// edge replaces — and a same-shaped edge on another board is not the same edge
const e1 = { board: "devtools", parent: "t_root", child: "t_c1" };
const e2 = { board: "renderpool", parent: "t_root", child: "t_c1" };
const toggleOk =
  hooks.sameEdge(hooks.nextEdge(null, e1), e1) &&
  hooks.nextEdge(e1, e1) === null &&
  hooks.sameEdge(hooks.nextEdge(e1, e2), e2) &&
  !hooks.sameEdge(null, e1);

// --- the click-to-detail drawer (ADR-0011, widened by ADR-0012) --------------
// Clicking a node or chip toggles a side window in the tab. The picture
// navigates nowhere: an anchor anywhere inside it fails the contract. The
// drawer draws the summary line first (pinned as drawn text, so a dropped
// field is a moved fact), then the fetched card — body, why it stopped, its
// result, newest events and comments, and the admitted session's labelled
// token breakdown. While the card is being read it draws a marked loading
// line, a card with no session says so, and a selection the rescan dropped
// resolves to no drawer at all. The tab owns this selection: same hook-count
// rule as the edge selection.
const CARD = {
  board: "devtools",
  id: "t_gchild",
  title: "t_gchild title",
  body: "Install the drift timer units and wire the check.",
  body_truncated: false,
  status: "running",
  assignee: "default",
  block_kind: null,
  last_failure_error: null,
  result: null,
  created_at: 1000,
  started_at: 1100,
  completed_at: null,
  branch_name: "master",
  workspace_path: "/home/dan/renderpool",
  session_id: "s2",
  events: [
    { kind: "completed", detail: "Fence verdict shipped in c441", created_at: 1700 },
    { kind: "commented", detail: "default", created_at: 1600 },
  ],
  comments: [
    { author: "dan", body: "ship it", created_at: 1750 },
    { author: "default", body: "gate green", created_at: 1720 },
  ],
  session: {
    title: "worker run",
    model: "qwen-max",
    source: "kanban",
    message_count: 42,
    tool_call_count: 17,
    last_activity_description: "wrote the drift timer unit",
    profile: "default",
    chat_link: "/chat?profile=default&resume=s2",
    tokens_in: 215800,
    tokens_out: 113296,
    tokens_cache_read: 1200000,
    tokens_cache_write: 30,
    tokens_reasoning: 55,
  },
};
const CARD_NOSESS = {
  board: "devtools",
  id: "t_c2",
  title: "t_c2 title",
  body: null,
  body_truncated: false,
  status: "review",
  assignee: "default",
  block_kind: "empty_result",
  last_failure_error: "worker returned nothing",
  result: null,
  created_at: 900,
  started_at: null,
  completed_at: null,
  branch_name: null,
  workspace_path: null,
  session_id: null,
  events: [{ kind: "created", detail: null, created_at: 900 }],
  comments: [],
  session: null,
};

function newAcc() {
  return {
    present: 0, loading: 0, close: 0, sumPresent: 0, activity: 0,
    anchors: [], placeholders: [], dots: [],
    body: "", events: "", comments: "", session: "", tokens: "", block: "",
    root: null, sumRoot: null,
  };
}
function detailWalk(node, acc) {
  if (Array.isArray(node)) {
    node.forEach(function (k) {
      detailWalk(k, acc);
    });
    return;
  }
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (props["data-detail"] !== undefined) {
    acc.present += 1;
    acc.root = node;
  }
  if (props["data-detail-loading"] !== undefined) acc.loading += 1;
  if (props["data-detail-close"] !== undefined && typeof props.onClick === "function") {
    acc.close += 1;
  }
  if (props["data-summary"] !== undefined) {
    acc.sumPresent += 1;
    acc.sumRoot = node;
  }
  if (node.type === "a" && props.href) acc.anchors.push(String(props.href));
  if (props["data-placeholder"] !== undefined) {
    acc.placeholders.push(String(props["data-placeholder"]));
  }
  if (props["data-status-dot"] !== undefined && props.style) {
    acc.dots.push(String(props.style.background));
  }
  if (props["data-detail-body"] !== undefined) acc.body = String(props["data-detail-body"]);
  if (props["data-detail-events"] !== undefined) acc.events = String(props["data-detail-events"]);
  if (props["data-detail-comments"] !== undefined) {
    acc.comments = String(props["data-detail-comments"]);
  }
  if (props["data-detail-session"] !== undefined) {
    acc.session = String(props["data-detail-session"]);
  }
  if (props["data-detail-tokens"] !== undefined) acc.tokens = String(props["data-detail-tokens"]);
  if (props["data-detail-block"] !== undefined) acc.block = String(props["data-detail-block"]);
  if (props["data-detail-activity"] !== undefined) acc.activity += 1;
  if (Array.isArray(node.kids)) node.kids.forEach(function (k) { detailWalk(k, acc); });
}

const opened = newAcc();
detailWalk(
  hooks.DetailPanel({
    found: hooks.findNode([graph], { board: "devtools", id: "t_gchild" }),
    card: CARD,
    onClose: function () {},
  }),
  opened
);

// no card answer yet: a marked loading line, never a confidently empty card
const loading = newAcc();
detailWalk(
  hooks.DetailPanel({
    found: hooks.findNode([graph], { board: "devtools", id: "t_gchild" }),
    card: null,
    onClose: function () {},
  }),
  loading
);

// a selected card the rescan no longer carries resolves to no drawer at all
const ghost = newAcc();
detailWalk(
  hooks.DetailPanel({
    found: hooks.findNode([graph], { board: "devtools", id: "t_nowhere" }),
    card: null,
    onClose: function () {},
  }),
  ghost
);

// a card the payload carries but no session has ever admitted: the drawer
// says so, and the summary line shows its marked placeholder, not a link
const noSess = newAcc();
detailWalk(
  hooks.DetailPanel({
    found: hooks.findNode([graph], { board: "devtools", id: "t_c2" }),
    card: CARD_NOSESS,
    onClose: function () {},
  }),
  noSess
);

// the same-node toggle mirrors the edge one, across boards included
const n1 = { board: "devtools", id: "t_c2" };
const n2 = { board: "renderpool", id: "t_c2" };
const nodeToggleOk =
  hooks.sameNode(hooks.nextNode(null, n1), n1) &&
  hooks.nextNode(n1, n1) === null &&
  hooks.sameNode(hooks.nextNode(n1, n2), n2) &&
  !hooks.sameNode(null, n1);

const sumText = opened.sumRoot ? textOf(opened.sumRoot).trim() : "";

// --- the shared column grid and the status colour cells (ADR-0009) ----------
// The two card sections of one board must land on one fixed column grid, so a
// Task/Title/Chat column sits at the same place in every section and every
// board card. And every status cell carries the palette dot beside its word.
function hasStatusDot(node) {
  if (!node || typeof node !== "object") return false;
  if (node.props && node.props["data-status-dot"] !== undefined) return true;
  return Array.isArray(node.kids) && node.kids.some(hasStatusDot);
}

function gridOf(node) {
  return (node.kids || [])
    .filter(function (k) {
      return k && k.type === "col";
    })
    .map(function (k) {
      return String(((k.props || {}).style || {}).width);
    })
    .join(",");
}

const cardGrids = [];
const cardStatusDots = [];
const cardStatusWords = [];
const cardTokens = [];
const cardCells = [];
let cardFixedTables = 0;
let statusPlaceholders = 0;
(function walkGrid(node) {
  if (Array.isArray(node)) {
    node.forEach(walkGrid);
    return;
  }
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (node.type === "table" && String(props.className || "").indexOf("table-fixed") >= 0) {
    cardFixedTables += 1;
  }
  if (node.type === "colgroup") cardGrids.push(gridOf(node));
  if (node.type === "td") {
    cardCells.push(textOf(node).trim());
    if (hasStatusDot(node)) cardStatusWords.push(textOf(node).trim());
  }
  // one marked tokens cell per row, in row order: the formatted counts (or the
  // placeholder for a session-less row) must be visible here, not just counted
  if (props["data-tokens"] !== undefined) cardTokens.push(textOf(node).trim());
  if (props["data-status-dot"] !== undefined) {
    cardStatusDots.push(
      String(props["data-status-dot"]) + ":" + String(((props.style || {}).background))
    );
  }
  // an empty status is the marked placeholder, never a bare fallback dot
  if (props["data-placeholder"] === "status") statusPlaceholders += 1;
  if (Array.isArray(node.kids)) node.kids.forEach(walkGrid);
})(tree);

// --- root chats: two human chats plus a row with nothing to resume -----------
// `link` is deliberately a profile the row does not carry: a bundle that builds
// the resume URL out of the row's own profile and session id lands on a
// different href, and the pinned chatHrefs move with it. The server composes the
// link (I19); the bundle only ever reads chat_link.
function chat(sessionId, title, source, activity, profile, link, tokens) {
  return {
    session_id: sessionId,
    title: title,
    source: source,
    profile: profile,
    last_activity_at: activity,
    chat_link: link === null ? null : "/chat?profile=" + link + "&resume=" + sessionId,
    tokens_in: tokens ? tokens[0] : null,
    tokens_out: tokens ? tokens[1] : null,
  };
}

const chats = [
  // a link without an activity time and an activity time without a link, so the
  // resume anchor cannot be keyed off the time cell by accident
  chat("s_human", "Review GPU pool work", "tui", null, "default", "watcher", [5000, 250]),
  chat("s_other", "(untitled)", "local", 2000, "researcher-a", "researcher-a", [1200, 640]),
  chat("s_noresume", "a chat with no link", "cron", 3000, "default", null),
];

const chatTree = hooks.RootChats({ chats: chats });
// an empty or partial payload must render the section with no rows, not throw
const emptyChatTree = hooks.RootChats({ chats: hooks.chatsOf(null) });

let chatCount = -1;
const chatHeadings = [];
const chatKeyFacts = [];
const chatAnchors = [];
const chatRowKeys = [];
const chatCells = [];
const chatGrids = [];
let chatFixedTables = 0;
let chatPlaceholders = 0;
(function walkChats(node) {
  if (Array.isArray(node)) {
    node.forEach(walkChats);
    return;
  }
  if (typeof node === "string" && node.indexOf("Root chats") === 0) chatHeadings.push(node);
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (node.type === "table" && String(props.className || "").indexOf("table-fixed") >= 0) {
    chatFixedTables += 1;
  }
  if (node.type === "colgroup") chatGrids.push(gridOf(node));
  if (props["data-chats"] !== undefined) chatCount = Number(props["data-chats"]);
  if (node.type === "tr" && props.key !== undefined) chatRowKeys.push(String(props.key));
  // the body cells, row by row: the title and source must reach the page and the
  // relative-time cell must be the one that calls the SDK's timeAgo
  if (node.type === "td") chatCells.push(textOf(node).trim());
  if (node.type === "a" && props.href) chatAnchors.push(String(props.href));
  if (props["data-placeholder"] === "chat") chatPlaceholders += 1;
  if (Array.isArray(node.kids)) node.kids.forEach(walkChats);
})(chatTree);

let emptyChatCount = -1;
let emptyChatRows = 0;
(function walkEmpty(node) {
  if (Array.isArray(node)) {
    node.forEach(walkEmpty);
    return;
  }
  if (!node || typeof node !== "object") return;
  const props = node.props || {};
  if (props["data-chats"] !== undefined) emptyChatCount = Number(props["data-chats"]);
  if (node.type === "tr" && props.key !== undefined) emptyChatRows += 1;
  if (Array.isArray(node.kids)) node.kids.forEach(walkEmpty);
})(emptyChatTree);

// every chat row is keyed by its session id, so a refresh that reorders rows
// cannot reuse the wrong row, and the count the card carries matches the rows
if (chatRowKeys.length !== chatCount) chatKeyFacts.push("count-mismatch");
for (const row of chats) {
  if (chatRowKeys.indexOf(row.session_id) < 0) chatKeyFacts.push("missing:" + row.session_id);
}
for (const key of chatRowKeys) {
  if (key === "undefined" || key === "") chatKeyFacts.push("blank-key");
}
chatKeyFacts.sort();

const wantChatAnchors = chats.filter((c) => c.chat_link).length;
const wantChatPlaceholders = chats.filter((c) => !c.chat_link).length;

// the chats table has five columns in the contract (title, source, tokens,
// last activity, resume), so the cell count is pinned against the row count
const CHAT_COLS = 5;

// chatsOf defends the render: a payload that has not arrived, or a chats field of
// the wrong shape, must give no rows rather than reach .map and throw inside React
const chatShapeFacts = [null, undefined, {}, { chats: "not-a-list" }]
  .map((bad, i) => (hooks.chatsOf(bad).length === 0 ? "" : "shape" + i))
  .filter(Boolean);

// the drawing must match the layout it was handed, and the x of a layer must
// grow with its depth — otherwise the "layered" layout is just a list
const layoutIds = layout.placed.map((p) => p.id).join(",");
const sameAsLayout = layoutIds === placedNodes.map((n) => n.id).join(",") ? "ok" : "mismatch";
const byLayer = {};
placedNodes.forEach((n) => {
  byLayer[n.depth] = Math.max(byLayer[n.depth] === undefined ? -1 : byLayer[n.depth], n.x);
});
const layerXs = Object.keys(byLayer)
  .map(Number)
  .sort((a, b) => a - b)
  .map((d) => byLayer[d]);
let geomOk = layerXs.length > 1;
for (let i = 1; i < layerXs.length; i += 1) {
  if (layerXs[i] <= layerXs[i - 1]) geomOk = false;
}
placedNodes.forEach((n) => {
  if (n.x < 0 || n.y < 0 || n.x > layout.width || n.y > layout.height) geomOk = false;
});

// two nodes of one layer must not sit on one row: stacked nodes read as a single
// card, and the card with the deep tree parent is exactly the case that does it
const rows = {};
let stacked = "";
placedNodes.forEach((n) => {
  const cell = n.depth + "@" + n.y;
  if (rows[cell]) stacked = rows[cell] + "+" + n.id;
  rows[cell] = n.id;
});

// a board status is untrusted text: a status like "constructor" must still get a
// colour rather than an inherited object member
const badColour = fills
  .concat(chipFills)
  .filter((entry) => !/^#[0-9a-f]{6}$/i.test(entry.split(":")[1] || ""));

// the picture navigates nowhere (ADR-0011): an anchor inside the graph is a
// contract break, and every placed node and unlinked chip must be clickable
console.log("boards=" + boards.join(","));
console.log("depths=" + depths.join(","));
console.log("anchors=" + anchors.length);
console.log("hrefs=" + anchors.join(","));
console.log("placeholders=" + placeholders);
console.log("query=" + query);
console.log("graphUrl=" + hooks.graphUrl({ board: "devtools" }));
console.log("graphSvg=" + svgs);
console.log("graphBoard=" + graphBoard);
console.log("graphLayers=" + placedNodes.map((n) => n.id + ":" + n.depth).join(","));
console.log("graphUnlinked=" + unlinkedShown.join(","));
console.log("graphPaths=" + graphPaths);
console.log("graphEdgePaths=" + edgeDirs.length);
console.log("graphHits=" + hitPaths);
console.log("graphClicks=" + hitClicks);
console.log("graphEdgeDirs=" + edgeDirs.join(","));
console.log("graphEdgeStrokes=" + edgeStrokes.join(","));
console.log("graphBackEdges=" + backLegend);
console.log("graphSelStyle=" + selAcc.selStyle);
console.log("graphSelDim=" + selAcc.dim);
console.log("graphSelNodes=" + selAcc.nodes);
console.log("graphGhostDim=" + ghostAcc.dim);
console.log("graphGhostNodes=" + ghostAcc.nodes);
console.log("edgeToggle=" + (toggleOk ? "ok" : "broken"));
console.log("graphDots=" + graphDots);
console.log("graphAnchors=" + graphAnchors.length);
console.log("graphHrefs=" + graphAnchors.join(","));
console.log("graphSame=" + sameAsLayout);
console.log("graphGeom=" + (geomOk ? "ok" : "bad"));
console.log("graphCells=" + placedNodes.map((n) => n.id + ":" + n.x + "," + n.y).join(","));
console.log("graphFills=" + fills.join(","));
console.log("graphChipFills=" + chipFills.join(","));
console.log("graphNodeClicks=" + nodeClicks);
console.log("graphChipClicks=" + chipClicks);
console.log("detailPresent=" + opened.present);
console.log("detailClose=" + opened.close);
console.log("detailOpenedLoading=" + opened.loading);
console.log("detailLoadingLine=" + loading.loading);
console.log("detailGhost=" + ghost.present);
console.log("detailBody=" + opened.body);
console.log("detailEvents=" + opened.events);
console.log("detailComments=" + opened.comments);
console.log("detailSession=" + opened.session);
console.log("detailActivity=" + opened.activity);
console.log("detailTokens=" + opened.tokens);
console.log("detailNoSessSession=" + noSess.session);
console.log("detailNoSessBlock=" + noSess.block);
console.log("detailNoSessPlaceholders=" + noSess.placeholders.join(","));
console.log("graphSummary=" + opened.sumPresent);
console.log("graphSummaryText=" + sumText);
console.log("graphSummaryAnchors=" + opened.anchors.length);
console.log("graphSummaryHrefs=" + opened.anchors.join(","));
console.log("graphSummaryDot=" + opened.dots.join(","));
console.log("graphNoSessionSummary=" + noSess.sumPresent);
console.log("graphNoSessionAnchors=" + noSess.anchors.length);
console.log("nodeToggle=" + (nodeToggleOk ? "ok" : "broken"));
console.log("chatsUrl=" + hooks.chatsUrl());
console.log("chatRows=" + chatCount);
console.log("chatRowKeys=" + chatRowKeys.join(","));
console.log("chatHeadings=" + chatHeadings.join("|"));
console.log("chatAnchors=" + chatAnchors.length);
console.log("chatHrefs=" + chatAnchors.join(","));
console.log("chatPlaceholders=" + chatPlaceholders);
console.log("chatEmptyRows=" + emptyChatRows + "/" + emptyChatCount);
console.log("chatWantAnchors=" + wantChatAnchors);
console.log("chatWantPlaceholders=" + wantChatPlaceholders);
console.log("chatKeyFacts=" + chatKeyFacts.join(","));
console.log("chatCells=" + chatCells.join("|"));
console.log("chatShapeFacts=" + chatShapeFacts.join(","));
console.log("cardRowKeys=" + cardRowKeys.join(","));
console.log("cardKeyFacts=" + cardKeyFacts.join(","));
console.log("chatsColgroups=" + chatGrids.join("|"));
console.log("chatsFixedTables=" + chatFixedTables);
console.log("cardColgroups=" + cardGrids.join("|"));
console.log("cardFixedTables=" + cardFixedTables);
console.log("cardStatusDots=" + cardStatusDots.join(","));
console.log("cardStatusWords=" + cardStatusWords.join("|"));
console.log("cardStatusPlaceholders=" + statusPlaceholders);
console.log("cardCells=" + cardCells.length);
console.log("cardTokens=" + cardTokens.join("|"));

if (depths.length === 0 || anchors.length === 0) {
  console.error("rendered tree carries no indent or no resume anchor");
  process.exit(3);
}
if (svgs === 0 || graphPaths === 0 || nodeClicks === 0 || graphDots === 0) {
  console.error("graph section draws no svg, edges, clickable nodes or status dots");
  process.exit(3);
}
if (unlinkedShown.length === 0 || placedNodes.length === 0) {
  console.error("graph section loses the unlinked cards or the layered nodes");
  process.exit(3);
}
if (sameAsLayout !== "ok" || !geomOk) {
  console.error("graph drawing does not match the layered layout");
  process.exit(3);
}
if (stacked) {
  console.error("two nodes of one layer sit on one row: " + stacked);
  process.exit(3);
}
if (badColour.length) {
  console.error("a status dot or unlinked chip has no colour: " + badColour.join(","));
  process.exit(3);
}
// the edge surface (ADR-0008): marked paths, a classified back edge, a distinct
// stroke per direction, one clickable hit path per edge
if (edgeDirs.length === 0) {
  console.error("the graph draws no marked edge paths (data-edge)");
  process.exit(3);
}
const backDirs = edgeDirs.filter(function (d) {
  return d.indexOf(":back") >= 0;
});
if (backDirs.length === 0) {
  console.error("no link back toward a shallower layer is classified as a back edge");
  process.exit(3);
}
if (backLegend !== backDirs.length) {
  console.error(
    "the return-edge legend says " + backLegend + " but " + backDirs.length + " are drawn back"
  );
  process.exit(3);
}
if (
  edgeDirs.some(function (d, i) {
    const stroke = edgeStrokes[i].slice(edgeStrokes[i].indexOf(":") + 1);
    return d.indexOf(":back") >= 0 ? stroke === "currentColor" : stroke !== "currentColor";
  })
) {
  console.error("a back edge and a forward edge share one stroke");
  process.exit(3);
}
if (hitPaths !== edgeDirs.length || hitClicks !== hitPaths) {
  console.error(
    "an edge lost its hit path or its click handler: " +
      edgeDirs.length +
      " edges, " +
      hitPaths +
      " hits, " +
      hitClicks +
      " handlers"
  );
  process.exit(3);
}
if (selAcc.selStyle !== "3@1" || selAcc.dim !== edgeDirs.length - 1 || selAcc.nodes !== 2) {
  console.error(
    "selecting an edge does not highlight, dim, or mark endpoints: " +
      selAcc.selStyle +
      " dim=" +
      selAcc.dim +
      " nodes=" +
      selAcc.nodes
  );
  process.exit(3);
}
if (ghostAcc.dim !== 0 || ghostAcc.nodes !== 0) {
  console.error("a selection the payload no longer carries still dims the board");
  process.exit(3);
}
if (!toggleOk) {
  console.error("the edge click-toggle does not select, clear, and replace cleanly");
  process.exit(3);
}
// the graph itself is click-to-detail, not click-to-chat: no anchor inside
// the picture, one handler per placed node and per unlinked chip, and the
// drawer carries the card's facts plus exactly one resume anchor
if (graphAnchors.length !== 0) {
  console.error("the graph still navigates on click: " + graphAnchors.length + " anchors drawn");
  process.exit(3);
}
if (nodeClicks !== placedNodes.length) {
  console.error(
    "placed nodes lost their summary click: " + nodeClicks + " of " + placedNodes.length
  );
  process.exit(3);
}
if (chipClicks !== unlinkedShown.length) {
  console.error("unlinked chips lost their summary click: " + chipClicks + " of " + unlinkedShown.length);
  process.exit(3);
}
if (opened.present !== 1 || opened.close !== 1) {
  console.error("the opened selection draws " + opened.present + " drawers and " + opened.close + " closes");
  process.exit(3);
}
if (opened.loading !== 0 || opened.body !== CARD.body || opened.events !== "completed,commented") {
  console.error("the drawer does not show the fetched card: " + opened.body + " / " + opened.events);
  process.exit(3);
}
if (opened.comments !== "2" || opened.session !== "1" || opened.activity !== 1) {
  console.error("the drawer lost its comments or session half");
  process.exit(3);
}
if (
  opened.tokens !==
  "sent 215,800 \u00b7 received 113,296 \u00b7 cache read 1,200,000 \u00b7 cache write 30 \u00b7 reasoning 55"
) {
  console.error("the drawer token breakdown is not labelled and exact: " + opened.tokens);
  process.exit(3);
}
if (loading.present !== 1 || loading.loading !== 1) {
  console.error("a drawer without its card answer draws no marked loading line");
  process.exit(3);
}
if (opened.sumPresent !== 1) {
  console.error("the drawer renders " + opened.sumPresent + " summary lines, expected 1");
  process.exit(3);
}
if (
  sumText !==
  "t_gchild | running — t_gchild title | layer 2 | parents: t_c2, t_a2 | " +
    "children: t_c1 | tokens: 216k/113k | resume in chat"
) {
  console.error("the summary line does not carry the node's facts: " + sumText);
  process.exit(3);
}
if (opened.anchors.length !== 1 || opened.anchors.join(",") !== "/chat?profile=default&resume=s2") {
  console.error("the drawer lost its one resume link: " + opened.anchors.join(","));
  process.exit(3);
}
if (opened.dots.join(",") !== "#3b82f6") {
  console.error("the summary status dot left the palette: " + opened.dots.join(","));
  process.exit(3);
}
if (noSess.present !== 1 || noSess.sumPresent !== 1 || noSess.anchors.length !== 0) {
  console.error("a sessionless selected card draws the wrong link story");
  process.exit(3);
}
if (noSess.placeholders.join(",") !== "chat,body") {
  console.error("the sessionless drawer lost its marked placeholders: " + noSess.placeholders.join(","));
  process.exit(3);
}
if (noSess.session !== "none" || noSess.block !== "empty_result") {
  console.error("the sessionless drawer hides the missing session or the block reason");
  process.exit(3);
}
if (ghost.present !== 0) {
  console.error("a selected card the payload no longer carries still renders a drawer");
  process.exit(3);
}
if (!nodeToggleOk) {
  console.error("the node click-toggle does not select, clear, and replace cleanly");
  process.exit(3);
}
if (chatHeadings.length === 0) {
  console.error("the root-chats section renders no heading");
  process.exit(3);
}
if (chatCount !== chats.length || chatAnchors.length === 0) {
  console.error("root chats render no rows or no resume anchor");
  process.exit(3);
}
if (chatAnchors.length !== wantChatAnchors) {
  console.error(
    "chat rows with a link lost their anchor: " + chatAnchors.length + " of " + wantChatAnchors
  );
  process.exit(3);
}
if (chatPlaceholders !== wantChatPlaceholders) {
  console.error("a chat row with nothing to resume lost its marked placeholder");
  process.exit(3);
}
if (chatKeyFacts.length) {
  console.error("chat rows are not keyed by session id: " + chatKeyFacts.join(","));
  process.exit(3);
}
if (chatCells.length !== chats.length * CHAT_COLS) {
  console.error(
    "the chats table renders " +
      chatCells.length +
      " cells for " +
      chats.length +
      " rows of " +
      CHAT_COLS +
      " columns"
  );
  process.exit(3);
}
if (chatShapeFacts.length) {
  console.error("chatsOf let a payload of the wrong shape through: " + chatShapeFacts.join(","));
  process.exit(3);
}
if (cardKeyFacts.length) {
  console.error("card table rows are not keyed by task id: " + cardKeyFacts.join(","));
  process.exit(3);
}
// the shared column grid and status colours (ADR-0009)
if (chatGrids.length !== 1 || chatFixedTables !== 1 || chatGrids[0].split(",").length !== CHAT_COLS) {
  console.error("the chats table lost its fixed column grid");
  process.exit(3);
}
if (cardGrids.length !== 2 || cardFixedTables !== 2 || cardGrids[0] !== cardGrids[1]) {
  console.error("the two card sections do not share one fixed column grid: " + cardGrids.join("|"));
  process.exit(3);
}
if (
  cardGrids[0].split(",").some(function (w) {
    return w === "auto" || w === "undefined";
  })
) {
  console.error("a shared-grid column has no fixed width: " + cardGrids[0]);
  process.exit(3);
}
if (cardStatusDots.length !== 5 || cardStatusWords.length !== 5) {
  console.error(
    "status cells lost their colour dot or their word: " +
      cardStatusDots.length +
      " dots, " +
      cardStatusWords.length +
      " words"
  );
  process.exit(3);
}
if (
  cardStatusDots.some(function (d) {
    return !/^#[0-9a-f]{6}$/i.test(d.slice(d.indexOf(":") + 1));
  })
) {
  console.error("a status cell dot has no colour: " + cardStatusDots.join(","));
  process.exit(3);
}
// exactly the one fixture row with an empty status carries the placeholder:
// not zero (a fallback dot claimed a status the row does not have) and not
// more (dots did not go missing, they multiplied)
if (statusPlaceholders !== 1) {
  console.error("empty status cells drawn wrong: " + statusPlaceholders + " placeholders");
  process.exit(3);
}
if (cardCells.length !== (group.running.length + group.recent.length) * 7) {
  console.error(
    "card tables render " +
      cardCells.length +
      " cells for " +
      (group.running.length + group.recent.length) +
      " rows of 7 columns"
  );
  process.exit(3);
}
// one tokens cell per card row, none of them blank: a dropped or unformatted
// count is a lost fact, not a cosmetic miss
if (cardTokens.length !== group.running.length + group.recent.length) {
  console.error("card tables render " + cardTokens.length + " tokens cells for 5 rows");
  process.exit(3);
}
if (
  cardTokens.some(function (t) {
    return t === "" || t === "undefined" || t === "null";
  })
) {
  console.error("a tokens cell is blank: " + cardTokens.join("|"));
  process.exit(3);
}
if (emptyChatRows !== 0 || emptyChatCount !== 0) {
  console.error("an empty chats payload did not render an empty section");
  process.exit(3);
}

// --- parked-card disarm and the pinned strip (ADR-0016, ADR-0017) -----------
// The Restart button follows the server's capability probe: available -> one
// button per parked row; unavailable -> the rows stay, every button goes.
const parkedRow = {
  id: "t_parked",
  title: "parked work",
  assignee: "default",
  status: "ready",
  error: "rate-limited run",
};
const parked = [{ board: "lora", rows: [parkedRow] }];

function stalledMarks(node) {
  const rows = [];
  const restarts = [];
  const offs = [];
  (function w(n) {
    if (!n || typeof n !== "object") return;
    if (n.props && "data-stalled-row" in n.props) rows.push(n.props["data-stalled-row"]);
    if (n.props && "data-restart" in n.props) restarts.push(n.props["data-restart"]);
    if (n.props && "data-restart-off" in n.props) offs.push(n.props["data-restart-off"]);
    (n.kids || []).forEach(w);
  })(node);
  return { rows: rows, restarts: restarts, offs: offs };
}

const stalledOn = stalledMarks(
  hooks.StalledTable({
    boards: parked,
    count: 1,
    messages: {},
    pending: {},
    log: [],
    retryAvailable: true,
    onRestart: function () {},
  })
);
const stalledOff = stalledMarks(
  hooks.StalledTable({
    boards: parked,
    count: 1,
    messages: {},
    pending: {},
    log: [],
    retryAvailable: false,
    onRestart: function () {},
  })
);
const retryOnFacts = [
  hooks.stalledRetryOn({ retry_available: true }),
  hooks.stalledRetryOn({ retry_available: false }),
  hooks.stalledRetryOn({}),
  hooks.stalledRetryOn(null),
].join(",");

console.log("stalledRowsOn=" + stalledOn.rows.join(","));
console.log("stalledAnchorsOn=" + stalledOn.restarts.length);
console.log("stalledAnchorsOff=" + stalledOff.restarts.length);
console.log("stalledRowsOff=" + stalledOff.rows.length);
console.log("stalledOffMarksOn=" + stalledOn.offs.length);
console.log("stalledOffMarksOff=" + stalledOff.offs.length);
console.log("retryOnFacts=" + retryOnFacts);

if (stalledOn.rows.length !== 1 || stalledOff.rows.length !== 1) {
  console.error("parked rows vanished under the probe flag");
  process.exit(3);
}
if (stalledOn.restarts.length !== 1 || stalledOff.restarts.length !== 0) {
  console.error("the Restart surface does not disarm with the probe flag");
  process.exit(3);
}
if (stalledOn.offs.length !== 0 || stalledOff.offs.length !== 1) {
  console.error("the unavailable placeholder does not follow the probe flag");
  process.exit(3);
}
if (retryOnFacts !== "true,false,false,false") {
  console.error("stalledRetryOn does not treat a missing key as unavailable: " + retryOnFacts);
  process.exit(3);
}

// --- pinned-chats strip (ADR-0017) ------------------------------------------
// The strip projects the same /chats rows the table below renders: only rows
// whose pinned flag is the boolean true appear, each row IS its resume anchor
// carrying the server-composed href, and a payload with nothing pinned renders
// no section at all — an absence, not an empty frame.
const pinnedChats = chats.map((c) => ({
  ...c,
  pinned: c.session_id === "s_human" || c.session_id === "s_other",
}));

// each marked row reads id@ when it is itself an <a> whose href resumes that
// very session, id! when the anchor is gone — the strip's whole point
function stripRows(node) {
  const rows = [];
  (function w(n) {
    if (!n || typeof n !== "object") return;
    if (n.props && "data-strip-row" in n.props) {
      const id = String(n.props["data-strip-row"]);
      const href = String(n.props.href || "");
      rows.push(id + (n.type === "a" && href.indexOf("resume=" + id) >= 0 ? "@" : "!"));
    }
    (n.kids || []).forEach(w);
  })(node);
  return rows;
}

const stripTree = hooks.PinnedStrip({ chats: hooks.pinnedOf({ chats: pinnedChats }) });
const stripRowFacts = stripRows(stripTree).join(",");
const stripStar = textOf(stripTree).indexOf("★") >= 0 ? 1 : 0;

// nothing pinned -> the component itself answers null, not an empty frame
const stripEmpty = hooks.PinnedStrip({ chats: hooks.pinnedOf({ chats: chats }) });
// pinnedOf reads the wrong-shape payloads chatsOf already defends, and reads a
// drifted string flag as UNpinned — only the strict boolean true pins a row
const pinnedShapeFacts = [null, undefined, {}, { chats: "not-a-list" }]
  .map((bad, i) => (hooks.pinnedOf(bad).length === 0 ? "" : "shape" + i))
  .filter(Boolean);
const stripStrict = hooks
  .pinnedOf({
    chats: [
      { session_id: "s_string", pinned: "true" },
      { session_id: "s_true", pinned: true },
    ],
  })
  .map((c) => c.session_id)
  .join(",");

console.log("stripRows=" + stripRowFacts);
console.log("stripStar=" + stripStar);
console.log("stripEmpty=" + (stripEmpty === null ? "null" : "rendered"));
console.log("stripStrict=" + stripStrict);

if (stripRowFacts !== "s_human@,s_other@") {
  console.error("the strip does not project exactly the pinned chats as resume anchors: " + stripRowFacts);
  process.exit(3);
}
if (stripStar !== 1) {
  console.error("the strip rows lost their star marker");
  process.exit(3);
}
if (stripEmpty !== null) {
  console.error("the strip renders an empty frame instead of vanishing");
  process.exit(3);
}
if (pinnedShapeFacts.length !== 0) {
  console.error("pinnedOf let a payload of the wrong shape through: " + pinnedShapeFacts.join(","));
  process.exit(3);
}
if (stripStrict !== "s_true") {
  console.error("pinnedOf reads a drifted flag as pinned: " + stripStrict);
  process.exit(3);
}
