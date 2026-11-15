# Worker Watch

A Hermes dashboard tab for watching kanban work as it happens: every running
card on every board, each linked to the chat session working on it, the card's
place in its parent → child tree, and the parked cards the dispatcher has
stopped spawning — plus this host's own root chats.

Read-only by design. The one write-shaped control, **Restart**, never touches a
board file: it asks Hermes' own CLI (`hermes kanban retry <card>`) to clear a
parked card's failure stamp — and only for the parked shape it exists for: an
assigned, unclaimed `ready` card whose failure text reads as a quota or
rate-limit block (`rate-limit`, `quota`, `HTTP 429`). Every other parked card is
listed with its reason and refused with a 409 instead of being restarted.

## What the tab shows

- **Running cards across boards** — one snapshot grouped per board, filtered by
  board, assignee and status. Each row carries the worker's profile, session id,
  a same-origin `/chat?profile=…&resume=…` link that resumes the session in the
  dashboard's chat tab, elapsed time and token counts.
- **Card hierarchy** — a card's `parent_id` and `depth` ride every row, so a
  board group renders as its parent → child → grandchild tree.
- **Card activity** — the drawer reads one card in full: status, assignee, block
  kind, failure stamp, body (capped at 4000 chars), the newest 8 events and
  newest 3 comments (each capped at 200/400 chars).
- **Parked cards** — cards the dispatcher stopped spawning: `ready`, assigned,
  and still carrying a failure stamp that `/watch` cannot show. This is where
  Restart lives.
- **This host's own root chats** — your local and `tui` sessions, pinned first,
  newest activity first, 25 at a time, each with its resume link.
- **Relationship graph** — *experimental*: one node/edge view per board, drawn
  as SVG. Expect layout and interaction changes.

## Routes

Mounted by the dashboard at `/api/plugins/worker-watch/`, behind the dashboard's
own sign-in:

| Route | What it answers |
| --- | --- |
| `GET /watch` | one snapshot: `board`, `assignee`, repeatable `status`, `limit` |
| `GET /graph` | one relationship graph per board (`board` narrows it) — experimental |
| `GET /chats` | root chats; repeatable `source` widens the filter past `local`/`tui` |
| `GET /card?board=&task=` | one card: facts, body, events, comments, session meta |
| `GET /stalled` | parked cards per board, plus `retry_available` |
| `POST /restart` | asks `hermes kanban retry` for one parked card; re-reads the row |

## Install

From the plugin catalog (Hermes ≥ 0.15.0):

```bash
hermes plugins install worker-watch
hermes plugins enable worker-watch
```

Or point Hermes at a checkout of this repository — the plugin root is the
repository root, so a local install is:

```bash
ln -s "$PWD" ~/.hermes/plugins/worker-watch
hermes plugins enable worker-watch
```

Restart the dashboard process after installing so the tab bundle is served:
`systemctl --user restart hermes-dashboard.service` (or restart `hermes
dashboard`). The tab appears at `/worker-watch`.

## Disclosure

What this plugin does with your data, in full:

- **Reads, all read-only.** Every board database under
  `<hermes home>/kanban/boards/*/kanban.db` and the session store
  `<hermes home>/state.db`, each opened through a SQLite `mode=ro` URI. It never
  opens a board or session store for writing, never creates a `-wal`/`-shm`
  sidecar, and a store it cannot open answers as empty instead of failing the
  page. The board tables it reads: `tasks`, `task_links`, `task_events`,
  `task_comments`, `task_runs`; from the session store: `sessions`.
- **One shell command, on one button press.** `POST /restart` runs
  `hermes kanban retry <board> <card>` as an explicit argv list (no shell, no
  string interpolation) and then re-reads the row. It refuses every card that is
  not the parked shape.
- **Restart stays hidden where the verb is absent.** `/stalled` answers
  `retry_available`, computed by probing the installed CLI; when the CLI has no
  `kanban retry` verb — stock Hermes today; the verb is a local overlay — the
  button is disarmed and restoring a credit is left to `hermes kanban unblock`
  or the dispatcher.
- **No network calls of its own, no telemetry, no credentials.** Nothing is sent
  off the machine, no API key or token is read, and the plugin registers no
  agent tools, hooks or middleware.
- **Caps, so one huge card cannot flood the drawer:** body 4000 chars, result
  1000, failure stamp 400, comment bodies 400, event detail 200, 8 events and 3
  comments per card, 25 root chats per page.
- **Reads only what the dashboard user can already see.** The routes are mounted
  behind the dashboard's authentication; a loopback dashboard is a loopback
  dashboard.

## Tests

`tests/` holds the suite (pytest, plus one Node harness for the bundle). The
read side is covered against fixture boards and session stores under `tmp_path`;
`tests/test_installed_layout.py` rebuilds the catalog install path from a clean
`git archive` checkout and asserts the six routes answer with the board and
session files byte-identical afterwards.

## License

MIT.
