(function () {
  "use strict";

  // Worker Watch — running kanban cards plus recent worker sessions across
  // every board (ADR-0002, extended by ADR-0004 and ADR-0005), this host's own
  // root chats (ADR-0007), and one relationship graph per board (ADR-0006). All
  // board data comes from one read-only snapshot: GET
  // /api/plugins/worker-watch/watch, filtered by board / status / assignee and
  // grouped by board server-side. Inside a board group each card carries
  // parent_id and depth, so the table renders the parent -> child -> grandchild
  // tree indented. Every row keeps its same-origin
  // /chat?profile=…&resume=… href, and nothing here writes.

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var Card = SDK.components.Card;
  var CardHeader = SDK.components.CardHeader;
  var CardTitle = SDK.components.CardTitle;
  var CardContent = SDK.components.CardContent;
  var Button = SDK.components.Button;
  var fetchJSON = SDK.fetchJSON;
  var timeAgo = SDK.utils.timeAgo;

  // Older host dashboards predate the design-system Select/Checkbox on the
  // plugin SDK, so fall back to native form elements and the tab still works.
  var Label = SDK.components.Label || "label";
  var Select = SDK.components.Select || "select";
  var SelectOption = SDK.components.SelectOption || "option";
  var Checkbox = SDK.components.Checkbox || function (props) {
    return h("input", {
      type: "checkbox",
      checked: !!props.checked,
      className: props.className,
      onClick: function (e) {
        if (props.onCheckedChange) props.onCheckedChange(e.target.checked);
      },
    });
  };

  // The DS Select reports onValueChange, a native <select> reports onChange;
  // wire both so one setter works with either host.
  function selectChangeHandler(setter) {
    return {
      onValueChange: function (v) { setter(v == null ? "" : v); },
      onChange: function (e) { setter(e && e.target ? e.target.value : (e == null ? "" : e)); },
    };
  }

  var WATCH_URL = "/api/plugins/worker-watch/watch";
  var GRAPH_URL = "/api/plugins/worker-watch/graph";
  var CHATS_URL = "/api/plugins/worker-watch/chats";
  var CARD_URL = "/api/plugins/worker-watch/card";
  // Parked cards (ADR-0013): the read the tab was missing, and the one write it
  // owns. A parked card is ready + assigned + still carrying a failure stamp —
  // the shape the dispatcher's respawn guard refuses forever, so it never shows
  // up in the running or history sections at all.
  var STALLED_URL = "/api/plugins/worker-watch/stalled";
  var RESTART_URL = "/api/plugins/worker-watch/restart";
  var REFRESH_MS = 15000;
  var STATUSES = ["running", "done", "review", "blocked"];

  // Relationship graph (ADR-0006): one SVG per board, layered left to right by
  // the depth the API computed. No graph library — the layout is a DFS over the
  // first-parent tree, which is all a board under ~100 nodes needs. ADR-0008
  // colours the links that run back toward a shallower layer, and lets one edge
  // per tab be clicked to trace: the tab owns that selection state, the picture
  // renders what it is handed.
  var NODE_W = 172;
  var NODE_H = 30;
  var GAP_X = 46;
  var GAP_Y = 14;
  var PAD = 10;
  // One status colour language across the tab (ADR-0009), on the mapping the
  // user pinned: running blue, done green, review yellow, blocked red. Graph
  // node dots, unlinked chips and table status cells all read from this table.
  var STATUS_FILL = {
    running: "#3b82f6",
    done: "#22c55e",
    review: "#facc15",
    blocked: "#ef4444",
    todo: "#94a3b8",
    ready: "#a3a3a3",
    triage: "#c084fc",
    archived: "#64748b",
  };

  // A board-supplied status must never reach the prototype chain: "constructor"
  // or "__proto__" would hand back an inherited member instead of a colour.
  function statusFill(status) {
    return Object.prototype.hasOwnProperty.call(STATUS_FILL, status)
      ? STATUS_FILL[status]
      : "#94a3b8";
  }

  // Token counts arrive as raw integers from the session store (ADR-0010) and
  // a table cell cannot carry six digits per column; one compact format, shared
  // by every Tokens cell, keeps the magnitudes comparable at a glance.
  function fmtTokens(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return null;
    if (n >= 1000000) return parseFloat((n / 1000000).toFixed(1)) + "M";
    if (n >= 10000) return Math.round(n / 1000) + "k";
    if (n >= 1000) return parseFloat((n / 1000).toFixed(1)) + "k";
    return String(Math.round(n));
  }

  // The compact cell cannot tell the reader which number is which, so the full
  // counts ride in a tooltip beside it (ADR-0012): sent to the model, received
  // back, and what the prompt cache read and wrote — a worker that re-read a
  // megabyte of cache every turn reads very differently from one that paid.
  function fmtExact(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return null;
    var s = String(Math.round(n)), out = "";
    while (s.length > 3) {
      out = "," + s.slice(-3) + out;
      s = s.slice(0, -3);
    }
    return s + out;
  }

  function tokensTip(r) {
    var parts = [];
    function add(label, v) {
      var e = fmtExact(v);
      if (e !== null) parts.push(label + " " + e);
    }
    add("sent", r.tokens_in);
    add("received", r.tokens_out);
    add("cache read", r.tokens_cache_read);
    add("cache write", r.tokens_cache_write);
    add("reasoning", r.tokens_reasoning);
    return parts.length ? parts.join(" \u00b7 ") : null;
  }

  function cardUrl(board, task) {
    var params = new URLSearchParams();
    params.set("board", board);
    params.set("task", task);
    return CARD_URL + "?" + params.toString();
  }

  // A link back toward a shallower place on the board (ADR-0008) is the curve a
  // reader most easily loses: the payload's BFS layer on each node says it
  // outright — child in the parent's own layer or above — and the layout then
  // draws that edge right-to-left. Those get their own stroke, and every drawn
  // edge carries an invisible wide hit stroke beside it, because a 1.5px curve
  // cannot be clicked. Clicking one selects it: thick and full opacity, the
  // other edges dim, and both endpoint nodes are marked.
  var EDGE_FWD_STROKE = "currentColor";
  var EDGE_BACK_STROKE = "#fb7185";
  var EDGE_HIT_WIDTH = 14;

  function edgeIsBack(parentNode, childNode) {
    return (childNode.depth || 0) <= (parentNode.depth || 0);
  }

  function edgeStyle(back, on, dimmed) {
    return {
      fill: "none",
      stroke: back ? EDGE_BACK_STROKE : EDGE_FWD_STROKE,
      strokeOpacity: on ? 1 : dimmed ? 0.12 : 0.45,
      strokeWidth: on ? 3 : dimmed ? 1 : 1.5,
      strokeDasharray: back ? "5 4" : "none",
    };
  }

  // One edge across the whole tab: the same parent/child on two boards is not
  // the same edge, so the identity carries the board.
  function sameEdge(a, b) {
    return !!a && !!b && a.board === b.board && a.parent === b.parent && a.child === b.child;
  }

  function nextEdge(prev, next) {
    return sameEdge(prev, next) ? null : next;
  }

  // The same toggle over a node selection (ADR-0011): clicking a node or an
  // unlinked chip opens its summary, clicking it again closes it, and a
  // same-shaped selection on another board is not the same selection.
  function sameNode(a, b) {
    return !!a && !!b && a.board === b.board && a.id === b.id;
  }

  function nextNode(prev, next) {
    return sameNode(prev, next) ? null : next;
  }

  function clip(text, max) {
    var s = String(text == null ? "" : text);
    return s.length > max ? s.slice(0, max - 1) + "\u2026" : s;
  }

  // One snapshot per endpoint; the graph takes only the board filter, because a
  // card's place in the graph does not depend on which statuses are ticked.
  function graphUrl(filters) {
    var params = new URLSearchParams();
    if (filters.board) params.set("board", filters.board);
    return GRAPH_URL + "?" + params.toString();
  }

  // Root chats (ADR-0007) come from the session store, which belongs to the host
  // and not to a board: the board/status/assignee filters do not narrow them, and
  // the server caps the section at its newest 25 root chats. Widening the source
  // filter (`?source=cron&source=oneshot`) is a backend knob only, so the tab
  // asks for the human chats and nothing else.
  function chatsUrl() {
    return CHATS_URL;
  }

  // Parked cards (ADR-0013) take only the board filter for the same reason the
  // graph does — a parked card is a board fact, not a status filter — and the
  // restart write takes the board and the task id, never a whole snapshot.
  function stalledUrl(filters) {
    var params = new URLSearchParams();
    if (filters.board) params.set("board", filters.board);
    return STALLED_URL + "?" + params.toString();
  }

  function stalledOf(payload) {
    var boards = payload && payload.boards;
    return Array.isArray(boards) ? boards : [];
  }

  // Whether this install speaks `kanban retry` (card t_1e9354dd): the payload's
  // probe key is the only signal, and a missing key reads as UNAVAILABLE — an
  // unknown capability must never offer a write the CLI would refuse.
  function stalledRetryOn(payload) {
    return !!payload && payload.retry_available === true;
  }

  // The rows of a chats payload, defensively: a payload that has not arrived yet,
  // or one from an older backend, reads as no rows rather than throwing in the
  // middle of a render.
  function chatsOf(payload) {
    var rows = payload && payload.chats;
    // a payload of the wrong shape (a string, an object) would reach rows.map and
    // throw inside the render: no rows is the only safe reading of a bad payload
    return Array.isArray(rows) ? rows : [];
  }

  // The owner's pinned chats, projected out of the same /chats rows (ADR-0017):
  // only the strict boolean true pins a row — a drifted flag value reads as
  // unpinned — and a wrong-shape payload reads as no pins, exactly like chatsOf.
  function pinnedOf(payload) {
    return chatsOf(payload).filter(function (c) {
      return !!c && c.pinned === true;
    });
  }

  function layerX(depth) {
    return PAD + depth * (NODE_W + GAP_X);
  }

  function layerY(slot) {
    return PAD + slot * (NODE_H + GAP_Y);
  }

  function nearestParent(incoming, byId) {
    // The tree edge a card hangs on is its nearest parent — the shallowest one —
    // because that is the same rule the backend used to choose the card's layer.
    // The first parent in id order is not it: it can sit in the card's own layer,
    // and a parent with a single tree child then takes that child's row exactly,
    // drawing two nodes on top of one another.
    var best = null;
    incoming.forEach(function (p) {
      var cand = byId[p];
      if (!cand) return; // an edge to a card that is gone is not a parent
      if (!best || cand.depth < best.depth || (cand.depth === best.depth && p < best.id)) {
        best = cand;
      }
    });
    return best;
  }

  // The layered layout: x comes from the payload's depth, and the vertical
  // order comes from a DFS of the tree each card hangs from, so a subtree stays
  // in one block. A leaf takes the next row; a parent sits between its children's
  // rows. Cards in no link at all are not placed in the picture — they go to
  // `unlinked`, which the section renders as its own mini-row. The visited set is
  // what keeps a corrupt cycle from walking forever, and a cycle card no root
  // reaches is still placed, as its own root.
  function graphLayout(graph) {
    var nodes = graph.nodes || [];
    var edges = graph.edges || [];
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });

    var kids = {};
    var parents = {};
    var linked = {};
    edges.forEach(function (e) {
      if (!byId[e.parent] || !byId[e.child]) return; // an edge to a gone card
      (kids[e.parent] = kids[e.parent] || []).push(e.child);
      (parents[e.child] = parents[e.child] || []).push(e.parent);
      linked[e.parent] = true;
      linked[e.child] = true;
    });

    var treeKids = {};
    var roots = [];
    nodes.forEach(function (n) {
      if (!linked[n.id]) return;
      var parent = nearestParent(parents[n.id] || [], byId);
      if (parent) {
        (treeKids[parent.id] = treeKids[parent.id] || []).push(n.id);
      } else {
        roots.push(n.id);
      }
    });

    var seen = {};
    var slot = {};
    var rows = 0;

    function place(id) {
      seen[id] = true;
      var children = (treeKids[id] || []).slice().sort().filter(function (c) {
        return !seen[c];
      });
      if (children.length === 0) {
        slot[id] = rows;
        rows += 1;
        return slot[id];
      }
      var total = 0;
      children.forEach(function (c) { total += place(c); });
      slot[id] = total / children.length;
      return slot[id];
    }

    roots.sort().forEach(place);
    // a cycle has no root above it: place what the roots never reached
    nodes.forEach(function (n) {
      if (linked[n.id] && !seen[n.id]) place(n.id);
    });

    var placed = nodes
      .filter(function (n) { return linked[n.id] && slot[n.id] !== undefined; })
      .sort(function (a, b) {
        if (a.depth !== b.depth) return a.depth - b.depth;
        if (slot[a.id] !== slot[b.id]) return slot[a.id] - slot[b.id];
        return a.id < b.id ? -1 : 1;
      })
      .map(function (n) {
        return {
          id: n.id,
          depth: n.depth || 0,
          row: slot[n.id],
          x: layerX(n.depth || 0),
          y: layerY(slot[n.id]),
        };
      });

    // One row per node inside a layer. A parent takes the mean of its children's
    // rows, and a cross edge can put two nodes of one layer on the same mean, so
    // rows are pushed down until each layer reads as distinct rows — two nodes at
    // the same x and y would be drawn as one.
    var lastRow = {};
    placed.forEach(function (p) {
      var floor = lastRow[p.depth];
      if (floor !== undefined && p.row <= floor) p.row = floor + 1;
      lastRow[p.depth] = p.row;
      p.y = layerY(p.row);
    });

    var layers = placed.reduce(function (m, p) { return Math.max(m, p.depth); }, 0);
    var used = placed.reduce(function (m, p) { return Math.max(m, p.row); }, 0);
    return {
      placed: placed,
      unlinked: nodes.filter(function (n) { return !linked[n.id]; }),
      width: PAD * 2 + (layers + 1) * NODE_W + layers * GAP_X,
      height: PAD * 2 + Math.max(used, rows - 1, 0) * (NODE_H + GAP_Y) + (NODE_H + GAP_Y),
    };
  }

  function edgePath(from, to) {
    var x1 = from.x + NODE_W;
    var y1 = from.y + NODE_H / 2;
    var x2 = to.x;
    var y2 = to.y + NODE_H / 2;
    var mid = (x1 + x2) / 2;
    return "M" + x1 + " " + y1 +
      " C" + mid + " " + y1 + ", " + mid + " " + y2 + ", " + x2 + " " + y2;
  }

  // One node of the picture: id, short title and a status-coloured dot. The
  // node is a click target for the summary panel (ADR-0011), not a navigation
  // anchor — the resume link lives inside the panel, where the card's other
  // facts are. A node on the selected edge gets a marked, heavier outline
  // (ADR-0008).
  function graphNodeShape(n, p, on, onClick) {
    return h("g", {
      key: "node-" + n.id,
      "data-node": n.id,
      "data-depth": p.depth,
      "data-x": p.x,
      "data-y": p.y,
      "data-edge-node": on ? "1" : undefined,
      style: { cursor: "pointer" },
      onClick: onClick,
      transform: "translate(" + p.x + "," + p.y + ")",
    },
      h("rect", {
        width: NODE_W, height: NODE_H, rx: 5,
        fill: "currentColor", fillOpacity: 0.08,
        stroke: "currentColor",
        strokeOpacity: on ? 0.9 : 0.35,
        strokeWidth: on ? 1.75 : 1,
      }),
      h("circle", { cx: 13, cy: NODE_H / 2, r: 4, fill: statusFill(n.status) }),
      h("text", { x: 24, y: 13, fontSize: 10 }, n.id),
      h("text", { x: 24, y: 25, fontSize: 9, fillOpacity: 0.65 }, clip(n.title, 22)));
  }

  // The summary one clicked node opens (ADR-0011): the card's facts in one
  // line — id, coloured status, title, its layer, the parents and children the
  // payload's edges give it, its token counts — and the resume link at the
  // end, so the click that used to jump straight to the chat now shows what
  // the card is before offering the jump. Empty pieces read as "none"/"—",
  // never as holes in the line.
  function summaryPanel(n, graph) {
    var parents = [], children = [];
    (graph.edges || []).forEach(function (e) {
      if (e.child === n.id) parents.push(e.parent);
      if (e.parent === n.id) children.push(e.child);
    });
    var tin = fmtTokens(n.tokens_in), tout = fmtTokens(n.tokens_out);
    var tokText = tin === null && tout === null
      ? "\u2014"
      : (tin === null ? "\u2014" : tin) + "/" + (tout === null ? "\u2014" : tout);
    var statusWord = n.status === null || n.status === undefined || n.status === ""
      ? "\u2014"
      : String(n.status);
    return h("div", {
      "data-summary": graph.board,
      className: "flex flex-wrap items-center gap-x-1 text-xs",
    },
      h("span", { className: "font-mono" }, n.id),
      " | ",
      h("span", {
        "data-status-dot": statusWord,
        style: {
          width: "0.5rem", height: "0.5rem", borderRadius: "9999px",
          background: statusFill(n.status), display: "inline-block",
        },
      }),
      statusWord,
      " \u2014 ",
      n.title || "(no title)",
      h("span", { className: "text-muted-foreground" },
        " | layer " + (n.depth || 0) +
        " | parents: " + (parents.length ? parents.join(", ") : "none") +
        " | children: " + (children.length ? children.join(", ") : "none") +
        " | tokens: " + tokText),
      n.chat_link
        ? h("a", { href: n.chat_link, className: "underline" }, " | resume in chat")
        : h("span", { className: "text-muted-foreground", "data-placeholder": "chat" },
            " | no session yet"));
  }

  // Resolve the tab's node selection against the live payload (ADR-0012): the
  // drawer draws a node the graph still carries, and a card the rescan dropped
  // — or a same-shaped selection from a board no longer on screen — resolves to
  // nothing, exactly like the stale edge and panel guards before it.
  function findNode(graphs, sel) {
    if (!sel) return null;
    for (var i = 0; i < graphs.length; i++) {
      var g = graphs[i];
      if (g.board !== sel.board) continue;
      var cards = (g.nodes || []).concat(g.unlinked || []);
      for (var j = 0; j < cards.length; j++) {
        if (cards[j].id === sel.id) return { graph: g, node: cards[j] };
      }
    }
    return null;
  }

  // The side window a node click opens — the kanban board's card drawer, read
  // only (ADR-0012). The graph node answers "where does this card sit"; the
  // drawer answers "what was it doing": the summary line, then the card's
  // body, why it stopped if it is blocked, its result, the newest events and
  // comments, and the admitted session's model, last activity and full labelled
  // token breakdown. While the card is being read the drawer draws its marked
  // loading line; a card with no admitted session says so instead of lying.
  function DetailPanel(props) {
    var found = props.found;
    if (!found) return null;
    var node = found.node, graph = found.graph;
    var card = props.card;
    var sess = card ? card.session : null;
    var meta = [];
    if (card) {
      if (card.assignee) meta.push("assignee " + card.assignee);
      if (card.branch_name) meta.push("branch " + card.branch_name);
      if (card.workspace_path) meta.push("in " + card.workspace_path);
      if (card.started_at) meta.push("started " + timeAgo(card.started_at));
      if (card.completed_at) meta.push("done " + timeAgo(card.completed_at));
    }
    return h(Card, { className: "w-[380px] shrink-0", "data-detail": graph.board + "/" + node.id },
      h(CardContent, { className: "space-y-2 text-xs" },
        h("div", { className: "flex items-center gap-2" },
          h("span", { className: "font-mono text-sm" }, node.id),
          h("span", { className: "flex-1" }),
          h(Button, {
            size: "sm", variant: "outline", onClick: props.onClose,
            "data-detail-close": "1",
          }, "close")),
        summaryPanel(node, graph),
        card === null
          ? h("p", { className: "text-muted-foreground", "data-detail-loading": "1" },
              "reading the card\u2026")
          : h("div", { className: "space-y-2" },
              h("div", { className: "text-sm" }, card.title || "(no title)"),
              card.body
                ? h("div", {
                    className: "whitespace-pre-wrap text-muted-foreground",
                    "data-detail-body": card.body.slice(0, 60),
                  }, card.body + (card.body_truncated ? " \u2026 (truncated)" : ""))
                : h("div", { className: "text-muted-foreground", "data-placeholder": "body" },
                    "no card body"),
              card.block_kind
                ? h("div", { "data-detail-block": card.block_kind },
                    "blocked: " + card.block_kind +
                    (card.last_failure_error ? " \u2014 " + card.last_failure_error : ""))
                : null,
              card.result
                ? h("div", { "data-detail-result": "1" }, "result: " + card.result)
                : null,
              meta.length
                ? h("div", { className: "text-muted-foreground", "data-detail-meta": meta.join(" \u00b7 ") },
                    meta.join(" \u00b7 "))
                : null,
              card.events && card.events.length
                ? h("div", null,
                    h("div", { className: "uppercase tracking-wide text-muted-foreground" }, "recent events"),
                    h("div", { "data-detail-events": card.events.map(function (e) { return e.kind; }).join(",") },
                      card.events.map(function (e, i) {
                        return h("div", { key: "ev" + i },
                          (e.created_at ? timeAgo(e.created_at) + " \u00b7 " : "") + e.kind +
                          (e.detail ? ": " + e.detail : ""));
                      })))
                : null,
              card.comments && card.comments.length
                ? h("div", null,
                    h("div", { className: "uppercase tracking-wide text-muted-foreground" }, "latest comments"),
                    h("div", { "data-detail-comments": String(card.comments.length) },
                      card.comments.map(function (c, i) {
                        return h("div", { key: "cm" + i }, (c.author || "?") + ": " + c.body);
                      })))
                : null,
              sess
                ? h("div", { className: "border-t border-border/40 pt-2 space-y-1", "data-detail-session": "1" },
                    h("div", null, "session: " + (sess.model ? sess.model + " \u00b7 " : "") + sess.title),
                    sess.last_activity_description
                      ? h("div", { className: "text-muted-foreground", "data-detail-activity": "1" },
                          "last activity: " + sess.last_activity_description)
                      : null,
                    h("div", { "data-detail-tokens": tokensTip(sess) || "\u2014" },
                      "tokens: " + (tokensTip(sess) || "\u2014")))
                : h("div", { className: "text-muted-foreground", "data-detail-session": "none" },
                    "no admitted session"))));
  }

  // One board's picture: the SVG, then the cards that are in no link at all, so
  // an unlinked card is listed rather than dropped or drawn as a lone island.
  // The selection arrives as props (ADR-0008): BoardGraph is called per board as
  // a plain function inside WatchPage's render, so state cannot live here — a
  // hook in a per-board call makes WatchPage's hook count vary as boards appear
  // and disappear. A selection the payload no longer carries reads as no
  // selection at all: a stale highlight must not dim a board with nothing to
  // point at.
  function BoardGraph(props) {
    var graph = props.graph;
    var nodes = graph.nodes || [];
    var layout = graphLayout(graph);
    var byId = {};
    nodes.forEach(function (n) { byId[n.id] = n; });
    var pos = {};
    layout.placed.forEach(function (p) { pos[p.id] = p; });

    var selected = null;
    if (props.selected && props.selected.board === graph.board) {
      var stillThere = (graph.edges || []).some(function (e) {
        return sameEdge(props.selected, { board: graph.board, parent: e.parent, child: e.child });
      });
      if (stillThere) selected = props.selected;
    }
    var endpoints = {};
    if (selected) {
      endpoints[selected.parent] = true;
      endpoints[selected.child] = true;
    }
    // the click target is the tab's node selection (ADR-0011); the drawer it
    // opens is drawn by the tab from the payload, not here (ADR-0012)
    function nodeClicked(n) {
      return function () {
        props.onSelectNode(nextNode(props.selectedNode, { board: graph.board, id: n.id }));
      };
    }

    var drawn = (graph.edges || [])
      .filter(function (e) { return pos[e.parent] && pos[e.child]; })
      .map(function (e) {
        return {
          edge: { board: graph.board, parent: e.parent, child: e.child },
          back: edgeIsBack(byId[e.parent], byId[e.child]),
          d: edgePath(pos[e.parent], pos[e.child]),
        };
      });
    var backCount = 0;
    drawn.forEach(function (item) {
      if (item.back) backCount += 1;
    });
    // the selected edge draws last: it stays on top of the curves it crosses
    drawn.sort(function (a, b) {
      return (sameEdge(selected, a.edge) ? 1 : 0) - (sameEdge(selected, b.edge) ? 1 : 0);
    });

    var paths = [];
    drawn.forEach(function (item) {
      var on = sameEdge(selected, item.edge);
      var style = edgeStyle(item.back, on, selected !== null && !on);
      var label = item.edge.parent + ">" + item.edge.child;
      paths.push(h("path", Object.assign(
        {
          key: label, d: item.d, "data-edge": label,
          "data-edge-dir": item.back ? "back" : "fwd",
        },
        style
      )));
      // the visible curve is 1.5px and uncatchable by a click: this transparent
      // wide stroke is what the pointer actually hits
      paths.push(h("path", {
        key: label + "|hit",
        d: item.d,
        "data-edge-hit": label,
        fill: "none", stroke: "transparent", strokeWidth: EDGE_HIT_WIDTH,
        pointerEvents: "stroke",
        style: { cursor: "pointer" },
        onClick: function () { props.onSelect(nextEdge(props.selected, item.edge)); },
      }));
    });

    return h(Card, { key: graph.board, "data-graph": graph.board },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-base" },
          graph.board + " \u2014 graph: " + layout.placed.length + " linked, " +
          layout.unlinked.length + " unlinked")),
      h(CardContent, { className: "space-y-3 overflow-x-auto p-0" },
        backCount
          ? h("div", {
              className: "px-3 text-xs text-muted-foreground",
              "data-back-edges": backCount,
            },
              "dashed rose arrows (" + backCount + ") link back toward a " +
              "shallower layer \u2014 click any arrow to trace it")
          : null,
        h("svg", {
          width: layout.width, height: layout.height,
          viewBox: "0 0 " + layout.width + " " + layout.height,
          className: "max-w-full", role: "img",
          "aria-label": graph.board + " task relationship graph",
        },
          paths,
          layout.placed.map(function (p) {
            return graphNodeShape(byId[p.id], p, !!endpoints[p.id], nodeClicked(byId[p.id]));
          })),
        layout.unlinked.length
          ? h("div", { className: "flex flex-wrap items-center gap-2 text-xs" },
              h("span", { className: "text-muted-foreground" },
                "unlinked \u2014 " + layout.unlinked.length),
              layout.unlinked.map(function (n) {
                return h("span", {
                  key: "chip-" + n.id,
                  "data-unlinked": n.id,
                  className: "inline-flex items-center gap-1 rounded border border-border/40 px-2 py-0.5",
                  style: { cursor: "pointer" },
                  onClick: nodeClicked(n),
                },
                  h("span", {
                    style: {
                      width: "0.5rem", height: "0.5rem", borderRadius: "9999px",
                      background: statusFill(n.status), display: "inline-block",
                    },
                  }),
                  n.id);
              }))
          : null));
  }

  // [key, label, kind, width] — kind: "tree" | "text" | "status" | "tokens" |
  // "ago" | "link"
  // The two card sections share one fixed column grid (ADR-0009): the same
  // columns in the same order at the same widths, so a Task/Title/Tokens/Chat
  // column lands at the same place in both sections of every board card. The
  // time column keeps its section's own label; the running section gained the
  // shared Status column (its rows always carry one). The "status" kind renders
  // the palette dot beside the word, so the cell colour matches the graph dot.
  // The Tokens column (ADR-0010) is the session's formatted in/out counts from
  // the store, or the placeholder when the row has no session or no counts.
  var RUNNING_COLS = [
    ["task_id", "Task", "tree", "12%"],
    ["title", "Title", "text", "22%"],
    ["status", "Status", "status", "10%"],
    ["assignee", "Assignee", "text", "10%"],
    ["tokens", "Tokens in/out", "tokens", "18%"],
    ["run_started_at", "Running for", "ago", "14%"],
    ["chat_link", "Chat", "link", "14%"],
  ];
  var RECENT_COLS = [
    ["task_id", "Task", "tree", "12%"],
    ["title", "Title", "text", "22%"],
    ["status", "Status", "status", "10%"],
    ["assignee", "Assignee", "text", "10%"],
    ["tokens", "Tokens in/out", "tokens", "18%"],
    ["session_at", "Session", "ago", "14%"],
    ["chat_link", "Chat", "link", "14%"],
  ];
  // A chat row has no card under it, so there is no tree column and no status:
  // what it is called, where it came from, what its session spent, when it last
  // moved, and the resume link. The resume header says what the cell does,
  // since the row already reads as a chat. Its own fixed grid, for the same
  // reason (ADR-0009).
  var CHATS_COLS = [
    ["title", "Title", "text", "32%"],
    ["source", "Source", "text", "12%"],
    ["tokens", "Tokens in/out", "tokens", "16%"],
    ["last_activity_at", "Last activity", "ago", "18%"],
    ["chat_link", "Resume", "link", "22%"],
  ];

  // The single query-string builder for the snapshot. `status` repeats per
  // checked status; an empty filter is simply left out of the query.
  function watchUrl(filters) {
    var params = new URLSearchParams();
    if (filters.board) params.set("board", filters.board);
    if (filters.assignee) params.set("assignee", filters.assignee);
    (filters.statuses || []).forEach(function (s) { params.append("status", s); });
    return WATCH_URL + "?" + params.toString();
  }

  // The tree column: indent by the card's real depth on its board and mark a
  // child, so a chain reads as a tree inside its board group.
  function treeCell(r) {
    var depth = r.depth || 0;
    return h("span", {
      "data-depth": r.depth,
      className: "whitespace-pre",
      style: { paddingLeft: (depth * 1.25) + "em" },
    }, (depth > 0 ? "\u2514\u2500 " : "") + r.task_id);
  }

  function cell(r, col) {
    var kind = col[2], value = r[col[0]];
    if (kind === "tree") {
      return treeCell(r);
    }
    if (kind === "status") {
      // the palette dot beside the word: the colour is carried by the dot, the
      // word stays in the theme's text colour, so both light and dark read it;
      // a row with no status word gets the placeholder alone — a lone
      // fallback-coloured dot would claim a status the row does not have
      var label = value === null || value === undefined || value === "" ? "" : String(value);
      if (label === "") {
        return h(
          "span",
          { className: "text-muted-foreground", "data-placeholder": "status" },
          "\u2014"
        );
      }
      return h("span", { className: "inline-flex items-center gap-1.5" },
        h("span", {
          "data-status-dot": label,
          style: {
            width: "0.5rem", height: "0.5rem", borderRadius: "9999px",
            background: statusFill(label), display: "inline-block",
          },
        }),
        label);
    }
    if (kind === "tokens") {
      // marked on both states — the counts and the placeholder — so a dropped
      // column is a missing cell in the harness, not a silently blank one; the
      // cache count joins the compact pair when it is real, and the exact
      // labelled counts ride in the title (ADR-0012)
      var tin = fmtTokens(r.tokens_in), tout = fmtTokens(r.tokens_out);
      if (tin === null && tout === null) {
        return h("span", { className: "text-muted-foreground", "data-tokens": "none" }, "—");
      }
      var shown = (tin === null ? "—" : tin) + "/" + (tout === null ? "—" : tout);
      var cre = fmtTokens(r.tokens_cache_read);
      if (cre !== null && r.tokens_cache_read > 0) shown += " \u00b7" + cre + "c";
      var tip = tokensTip(r);
      return h("span", { "data-tokens": shown, title: tip || shown }, shown);
    }
    if (kind === "link") {
      return value
        ? h("a", { href: r.chat_link, className: "underline" }, "resume in chat")
        : h("span", { className: "text-muted-foreground", "data-placeholder": "chat" },
            "no session yet");
    }
    if (kind === "ago") {
      return h("span", { className: "text-muted-foreground" },
        value ? timeAgo(value) : "—");
    }
    return value === null || value === undefined || value === ""
      ? h("span", { className: "text-muted-foreground" }, "—")
      : String(value);
  }

  function SectionTable(props) {
    var rows = props.rows;
    var cols = props.cols;
    // A card row is keyed by its task id; a chat row has no card, so the section
    // says which field identifies it. A row keyed by a field it does not have
    // carries no key at all, and a refresh that reorders rows then reuses the
    // wrong one.
    var keyField = props.keyField || "task_id";
    return h("div", { className: "space-y-1" },
      h("div", { className: "px-3 text-xs uppercase tracking-wide text-muted-foreground" },
        props.title + " — " + rows.length),
      h("table", { className: "w-full text-sm table-fixed" },
        // the fixed grid (ADR-0009): widths come from the column table, so the
        // two card sections share one grid and columns align across every view
        h("colgroup", null,
          cols.map(function (c) {
            return h("col", { key: c[0], style: { width: c[3] || "auto" } });
          })),
        h("thead", null,
          h("tr", { className: "text-left text-xs text-muted-foreground" },
            cols.map(function (c) {
              return h("th", { key: c[0], className: "px-3 py-1" }, c[1]);
            }))),
        h("tbody", null,
          rows.map(function (r) {
            return h("tr", { key: r[keyField], className: "border-t border-border/40" },
              cols.map(function (c) {
                // long titles wrap; every other column stays on one line
                var wrap = c[0] === "title" ? "" : " whitespace-nowrap";
                return h("td", { key: c[0], className: "px-3 py-1" + wrap }, cell(r, c));
              }));
          }))));
  }

  // One board group: the header names the board, and each section renders its
  // rows in the server's tree order — roots first, children indented.
  function BoardGroup(props) {
    var group = props.group;
    var running = group.running || [];
    var recent = group.recent || [];
    return h(Card, { key: group.board, "data-group": group.board },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-base" },
          group.board + " — " + running.length + " running, " + recent.length + " recent")),
      h(CardContent, { className: "space-y-4 overflow-x-auto p-0" },
        running.length
          ? SectionTable({ title: "Running cards", rows: running, cols: RUNNING_COLS })
          : null,
        recent.length
          ? SectionTable({ title: "Recent sessions", rows: recent, cols: RECENT_COLS })
          : null));
  }

  // The third section (ADR-0007): this host's own chats. The read already dropped
  // subagent runs, worker and timer sessions, archived and hidden sessions, and
  // anything that hangs below a parent session, and capped the rest newest
  // activity first — so the section renders what it is handed, one resume link
  // per row, built server-side like every other link here.
  function RootChats(props) {
    var chats = props.chats || [];
    return h(Card, { "data-chats": chats.length },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-base" },
          "Root chats — this host's own sessions: " + chats.length)),
      h(CardContent, { className: "space-y-1 overflow-x-auto p-0" },
        chats.length
          ? SectionTable({
              title: "Root chats",
              rows: chats,
              cols: CHATS_COLS,
              keyField: "session_id",
            })
          : h("p", { className: "px-3 text-sm text-muted-foreground" },
              "no root chats on this host")));
  }

  // The pinned-chats strip (ADR-0017): the owner's stars sit at the very top of
  // the page, above every table, so a working chat is never buried by newer
  // traffic. Each row IS its resume anchor, carrying the server-composed href
  // (I19 — the bundle never builds a chat URL). Nothing pinned renders nothing:
  // an absence, not an empty frame.
  function PinnedStrip(props) {
    var chats = props.chats;
    if (!Array.isArray(chats) || chats.length === 0) return null;
    return h("div", { className: "space-y-1", "data-pinned-strip": chats.length },
      h("div", { className: "px-1 text-xs uppercase tracking-wide text-muted-foreground" },
        "Pinned chats"),
      h("div", { className: "flex flex-wrap gap-2" },
        chats.map(function (c) {
          return h("a", {
            key: c.session_id,
            href: c.chat_link,
            "data-strip-row": c.session_id,
            className: "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs hover:bg-accent",
          },
            h("span", { "aria-hidden": "true", className: "text-amber-400" }, "★"),
            c.title || "(untitled)");
        })));
  }

  // The parked-card section (ADR-0013): the one list that can show a card the
  // dispatcher has stopped spawning — ready, assigned, still stamped with the
  // failure text the respawn guard reads as a quota blocker. Each row carries a
  // Restart button; this renderer is called once per board, so it holds no hooks
  // of its own: the pending map and the per-row answer arrive as props.
  var STALLED_COLS = [
    ["id", "Card", "id", "12%"],
    ["title", "Title", "title", ""],
    ["assignee", "Assignee", "assignee", "12%"],
    ["error", "Failure stamp", "error", "38%"],
  ];

  function StalledTable(props) {
    var boards = props.boards || [];
    var messages = props.messages || {};
    var pending = props.pending || {};
    return h(Card, { "data-stalled": boards.length },
      h(CardHeader, { className: "pb-2" },
        h(CardTitle, { className: "text-base" },
          "Parked cards — ready, not being spawned: " + props.count)),
      h(CardContent, { className: "space-y-3 overflow-x-auto p-0" },
        props.log && props.log.length
          ? h("div", { className: "px-3 text-xs", "data-restart-log": props.log.length },
              props.log.map(function (m) {
                return h("div", {
                  key: m.key + (m.ok ? "+" : "-") + m.text,
                  className: m.ok ? "text-emerald-400" : "text-red-400",
                }, m.key + " — " + m.text);
              }))
          : null,
        boards.length === 0
          ? h("p", { className: "px-3 text-sm text-muted-foreground" },
              "no parked card: every assigned ready card is spawnable")
          : boards.map(function (g) {
              return h("div", { key: g.board, className: "space-y-1", "data-stalled-board": g.board },
                h("div", { className: "px-3 text-xs uppercase tracking-wide text-muted-foreground" },
                  g.board + " — " + g.rows.length),
                h("table", { className: "w-full text-sm table-fixed" },
                  h("colgroup", null,
                    STALLED_COLS.map(function (c) {
                      return h("col", { key: c[0], style: { width: c[3] || "auto" } });
                    }),
                    h("col", { key: "action", style: { width: "14%" } })),
                  h("thead", null,
                    h("tr", { className: "text-left text-xs text-muted-foreground" },
                      STALLED_COLS.map(function (c) {
                        return h("th", { key: c[0], className: "px-3 py-1" }, c[1]);
                      }),
                      h("th", { key: "action", className: "px-3 py-1" }, "Restart"))),
                  h("tbody", null,
                    g.rows.map(function (r) {
                      var key = g.board + "/" + r.id;
                      var msg = messages[key];
                      return h("tr", { key: r.id, className: "border-t border-border/40",
                          "data-stalled-row": key },
                        h("td", { className: "px-3 py-1 whitespace-nowrap" }, r.id),
                        h("td", { className: "px-3 py-1" }, r.title),
                        h("td", { className: "px-3 py-1 whitespace-nowrap" }, r.assignee),
                        h("td", { className: "px-3 py-1 text-xs text-muted-foreground" },
                          clip(r.error, 140)),
                        h("td", { className: "px-3 py-1 whitespace-nowrap" },
                          props.retryAvailable === true
                            ? h(Button, {
                                size: "sm",
                                variant: "outline",
                                disabled: !!pending[key],
                                onClick: function () { props.onRestart(g.board, r.id); },
                                "data-restart": key,
                          }, pending[key] ? "Restarting…" : "Restart")
                            : h("span", { className: "text-xs text-muted-foreground",
                                "data-restart-off": key }, "unavailable"),
                          msg
                            ? h("span", {
                                className: msg.ok
                                  ? "ml-2 text-xs text-emerald-400"
                                  : "ml-2 text-xs text-red-400",
                                "data-restart-msg": key,
                              }, msg.text)
                            : null));
                    }))));
            })));
  }

  function FilterBar(props) {
    var f = props.filters;
    var boards = props.data.board_options || [];
    var assignees = props.data.assignee_options || [];

    function pick(over) {
      var next = { board: f.board, assignee: f.assignee, statuses: f.statuses.slice() };
      Object.assign(next, over);
      props.onChange(next);
    }
    function toggleStatus(s) {
      var checked = f.statuses.indexOf(s) >= 0;
      pick({
        statuses: checked
          ? f.statuses.filter(function (x) { return x !== s; })
          : f.statuses.concat([s]),
      });
    }

    return h(Card, null,
      h(CardContent, { className: "flex flex-wrap items-center gap-x-4 gap-y-2 text-xs" },
        h(Label, { className: "text-muted-foreground" }, "Board"),
        h(Select, Object.assign(
          { value: f.board, className: "h-8" },
          selectChangeHandler(function (v) { pick({ board: v }); })
        ),
          h(SelectOption, { value: "" }, "all boards"),
          boards.map(function (b) {
            return h(SelectOption, { key: b, value: b }, b);
          })),
        h(Label, { className: "text-muted-foreground" }, "Assignee"),
        h(Select, Object.assign(
          { value: f.assignee, className: "h-8" },
          selectChangeHandler(function (v) { pick({ assignee: v }); })
        ),
          h(SelectOption, { value: "" }, "any assignee"),
          assignees.map(function (a) {
            return h(SelectOption, { key: a, value: a }, a);
          })),
        h(Label, { className: "text-muted-foreground" }, "Status"),
        STATUSES.map(function (s) {
          return h(Label, { key: s, className: "flex items-center gap-1" },
            h(Checkbox, {
              checked: f.statuses.indexOf(s) >= 0,
              onCheckedChange: function () { toggleStatus(s); },
            }),
            s);
        })));
  }

  function WatchPage() {
    var _data = useState(null);
    var data = _data[0], setData = _data[1];
    var _graphData = useState(null);
    var graphData = _graphData[0], setGraphData = _graphData[1];
    var _chatData = useState(null);
    var chatData = _chatData[0], setChatData = _chatData[1];
    var _err = useState(null);
    var error = _err[0], setError = _err[1];
    var _load = useState(false);
    var loading = _load[0], setLoading = _load[1];
    var _filters = useState({ board: "", assignee: "", statuses: STATUSES.slice() });
    var filters = _filters[0], setFilters = _filters[1];
    // One traced edge across the whole tab (ADR-0008), held here rather than in
    // BoardGraph: per-board renderers are plain function calls in this render,
    // and their hook count varies as boards appear and disappear. The rescan
    // replaces graphData underneath, and the selection outlives it as long as
    // the edge is still on the board.
    var _sel = useState(null);
    var selectedEdge = _sel[0], setSelectedEdge = _sel[1];
    // and one open node summary per tab (ADR-0011), under the same rule
    var _selNode = useState(null);
    var selectedNode = _selNode[0], setSelectedNode = _selNode[1];
    // the drawer's fetched card (ADR-0012), keyed so a late answer for a card
    // the tab already moved away from is dropped rather than shown
    var _card = useState(null);
    var cardState = _card[0], setCardState = _card[1];
    // Parked cards (ADR-0013): the read, the per-row restart answers and the
    // in-flight set all live here, because StalledTable is a plain per-board
    // function call and must not own hooks.
    var _stalled = useState(null);
    var stalledData = _stalled[0], setStalledData = _stalled[1];
    var _restartMsg = useState({});
    var restartMsg = _restartMsg[0], setRestartMsg = _restartMsg[1];
    var _restartBusy = useState({});
    var restartBusy = _restartBusy[0], setRestartBusy = _restartBusy[1];
    // The answer has to outlive the row it came from: a successful restart removes
    // the card from the parked list on the reload, so a per-row message would be
    // unmounted before anyone reads it. The tab keeps the last answers instead.
    var _restartLog = useState([]);
    var restartLog = _restartLog[0], setRestartLog = _restartLog[1];

    var load = useCallback(function () {
      if (filters.statuses.length === 0) {
        // No status box checked means "show nothing", but a missing status
        // param reads as "every status" to the API — so answer that case here
        // and keep the filter bar's options from the last real snapshot.
        setLoading(false);
        setData(function (prev) {
          return {
            generated_at: null,
            boards: [],
            board_options: (prev && prev.board_options) || [],
            assignee_options: (prev && prev.assignee_options) || [],
          };
        });
        setError(null);
        return Promise.resolve();
      }
      setLoading(true);
      return fetchJSON(watchUrl(filters))
        .then(function (d) { setData(d); setError(null); })
        .catch(function (err) { setError(String(err)); })
        .finally(function () { setLoading(false); });
    }, [filters]);

    // The graph is its own read (ADR-0006) and follows only the board filter: a
    // card's place in the graph does not depend on the status checkboxes. It
    // fails quietly — the tables are the primary surface, and a graph section
    // that cannot load must not blank them or claim the API is unreachable. What
    // it must not do is drop the picture it already drew: the last good graph
    // stays on screen and the rescan warning below says what failed.
    var loadGraph = useCallback(function () {
      return fetchJSON(graphUrl({ board: filters.board }))
        .then(function (d) { setGraphData(d); })
        .catch(function () { /* keep the last good graph */ });
    }, [filters.board]);

    // The chats are their own read (ADR-0007) with no filter at all: the session
    // store is host-wide. It fails quietly for the same reason the graph does —
    // the tables are the primary surface — and keeps the rows it already drew.
    var loadChats = useCallback(function () {
      return fetchJSON(chatsUrl())
        .then(function (d) { setChatData(d); })
        .catch(function () { /* keep the last good chats */ });
    }, []);

    // Parked cards (ADR-0013) are their own read and fail quietly like the graph:
    // the sections above are the primary surface, and a restart button must never
    // be drawn from a payload that failed to load.
    var loadStalled = useCallback(function () {
      return fetchJSON(stalledUrl(filters))
        .then(function (d) { setStalledData(d); })
        .catch(function () { /* keep the last good stalled list */ });
    }, [filters.board]);

    // One restart per card at a time, and the answer is printed on the row: the
    // write is the CLI's single `kanban retry` leg, so a refusal (a card that is not
    // parked) is a normal outcome the operator reads, not a crash to hide.
    var restartCard = useCallback(function (board, taskId) {
      var key = board + "/" + taskId;
      setRestartBusy(function (prev) {
        var next = Object.assign({}, prev);
        next[key] = true;
        return next;
      });
      setRestartMsg(function (prev) {
        var next = Object.assign({}, prev);
        delete next[key];
        return next;
      });
      return fetchJSON(RESTART_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ board: board, task_id: taskId }),
      }).then(function () {
        setRestartMsg(function (prev) {
          var next = Object.assign({}, prev);
          next[key] = { ok: true, text: "stamp cleared — claimed within 60 s" };
          return next;
        });
        setRestartLog(function (prev) {
          return [{ key: key, ok: true, text: "stamp cleared — claimed within 60 s" }]
            .concat(prev).slice(0, 5);
        });
      }).catch(function (err) {
        var text = String(err && err.message ? err.message : err);
        setRestartMsg(function (prev) {
          var next = Object.assign({}, prev);
          next[key] = { ok: false, text: text };
          return next;
        });
        setRestartLog(function (prev) {
          return [{ key: key, ok: false, text: text }].concat(prev).slice(0, 5);
        });
      }).finally(function () {
        setRestartBusy(function (prev) {
          var next = Object.assign({}, prev);
          delete next[key];
          return next;
        });
        loadStalled();
      });
    }, [loadStalled]);

    // The drawer reads its whole card once per selection (ADR-0012). The fetch
    // lives in the tab because per-board renderers cannot own state; while the
    // read is in flight or has failed the drawer draws its marked loading line
    // — a failed read must not draw a confidently empty card.
    var cardKey = selectedNode ? selectedNode.board + "/" + selectedNode.id : null;
    useEffect(function () {
      if (!selectedNode) {
        setCardState(null);
        return undefined;
      }
      var alive = true;
      setCardState({ key: cardKey, data: null });
      fetchJSON(cardUrl(selectedNode.board, selectedNode.id)).then(
        function (d) { if (alive) setCardState({ key: cardKey, data: d }); },
        function () { if (alive) setCardState({ key: cardKey, data: null }); }
      );
      return function () { alive = false; };
    }, [selectedNode, cardKey]);

    useEffect(function () {
      load();
      loadGraph();
      loadChats();
      loadStalled();
      var t = setInterval(function () { load(); loadGraph(); loadChats(); loadStalled(); }, REFRESH_MS);
      return function () { clearInterval(t); };
    }, [load, loadGraph, loadChats, loadStalled]);

    if (error && !data) {
      return h(Card, null,
        h(CardContent, { className: "text-sm text-red-400" },
          "worker-watch API unreachable: " + error +
          " — the backend routes mount at dashboard start (or on first request" +
          " once the plugin is in plugins.enabled); check the dashboard logs."));
    }
    if (!data) return h("p", { className: "text-sm text-muted-foreground" },
      "scanning boards…");

    var groups = data.boards || [];
    var graphs = (graphData && graphData.boards) || [];
    var drawerFound = findNode(graphs, selectedNode);
    return h("div", { className: "space-y-4" },
      PinnedStrip({ chats: pinnedOf(chatData) }),
      h(Card, null,
        h(CardContent, { className: "flex items-center gap-3 text-xs text-muted-foreground" },
          h(Button, {
            size: "sm", variant: "outline",
            onClick: function () { load(); loadGraph(); loadChats(); loadStalled(); },
            disabled: loading,
          }, loading ? "Rescanning…" : "Rescan boards"),
          h("span", null, groups.length + " board" + (groups.length === 1 ? "" : "s") +
            " with cards"),
          data.generated_at ? h("span", null, "snapshot " + timeAgo(data.generated_at)) : null)),
      FilterBar({ filters: filters, data: data, onChange: setFilters }),
      stalledData === null
        ? null
        : StalledTable({
            boards: stalledOf(stalledData),
            count: (stalledData && stalledData.count) || 0,
            pending: restartBusy,
            messages: restartMsg,
            log: restartLog,
            onRestart: restartCard,
            retryAvailable: stalledRetryOn(stalledData),
          }),
      groups.length === 0
        ? h(Card, null, h(CardContent, { className: "text-sm text-muted-foreground" },
            "no card matches these filters on any board"))
        : groups.map(function (g) { return BoardGroup({ group: g }); }),
      chatData === null
        ? h("p", { className: "text-sm text-muted-foreground" }, "scanning this host's chats…")
        : RootChats({ chats: chatsOf(chatData) }),
      h("div", { className: "space-y-3" },
        h("div", { className: "px-1 text-xs uppercase tracking-wide text-muted-foreground" },
          "Relationship graph — task_links per board"),
        graphData === null
          ? h("p", { className: "text-sm text-muted-foreground" }, "scanning board graphs…")
          : graphs.length === 0
            ? h(Card, null, h(CardContent, { className: "text-sm text-muted-foreground" },
                "no board has any card to graph"))
            : h("div", { className: "flex flex-wrap items-start gap-4" },
                h("div", { className: "flex-1 space-y-3" },
                  graphs.map(function (g) {
                    return BoardGraph({
                      graph: g,
                      selected: selectedEdge,
                      onSelect: setSelectedEdge,
                      selectedNode: selectedNode,
                      onSelectNode: setSelectedNode,
                    });
                  })),
                // the side window the click opened, beside the pictures: it
                // closes when the card leaves the payload, and the tab hands it
                // the fetched card only while the selection still matches
                drawerFound
                  ? DetailPanel({
                      found: drawerFound,
                      card: cardState && cardState.key === cardKey ? cardState.data : null,
                      onClose: function () { setSelectedNode(null); },
                    })
                  : null)),
      error ? h("p", { className: "text-xs text-amber-400" },
        "last rescan failed: " + error) : null);
  }

  // Pin surface for tests/js/watch_bundle_harness.mjs: the harness renders this
  // tree through a stub SDK, so the indent, the resume href on every row, the
  // filter query strings, the graph's layered layout and SVG surface, and the
  // root-chats table (rows, keys, anchors, placeholder) are checked without a
  // browser. The dashboard never reads this global.
  window.__HERMES_WORKER_WATCH__ = {
    watchUrl: watchUrl,
    graphUrl: graphUrl,
    graphLayout: graphLayout,
    chatsUrl: chatsUrl,
    chatsOf: chatsOf,
    pinnedOf: pinnedOf,
    PinnedStrip: PinnedStrip,
    stalledUrl: stalledUrl,
    stalledOf: stalledOf,
    stalledRetryOn: stalledRetryOn,
    StalledTable: StalledTable,
    BoardGroup: BoardGroup,
    BoardGraph: BoardGraph,
    RootChats: RootChats,
    edgeIsBack: edgeIsBack,
    edgeStyle: edgeStyle,
    sameEdge: sameEdge,
    nextEdge: nextEdge,
    sameNode: sameNode,
    nextNode: nextNode,
    fmtTokens: fmtTokens,
    fmtExact: fmtExact,
    tokensTip: tokensTip,
    cardUrl: cardUrl,
    findNode: findNode,
    DetailPanel: DetailPanel,
  };

  window.__HERMES_PLUGINS__.register("worker-watch", WatchPage);
})();
