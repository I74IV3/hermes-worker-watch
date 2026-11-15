# hermes-worker-watch

Worker Watch is a Hermes dashboard tab for watching kanban work as it happens:
every running card on every board, each linked to the chat session working on
it, each card's place in its parent → child tree, one card's full activity in a
drawer, the parked cards the dispatcher has stopped spawning, this host's own
root chats, and one relationship graph per board (experimental).

The plugin lives in [`plugin/worker-watch/`](plugin/worker-watch). That
directory is the plugin root — it holds `plugin.yaml`, the native entry
(`__init__.py`), the dashboard tab (`dashboard/`) and the `workerwatch` read
package — and it is the subdir the plugin-catalog entry installs.

Read the plugin's own README for what the tab shows, the six routes, the install
steps and the full disclosure:
[`plugin/worker-watch/README.md`](plugin/worker-watch/README.md).

## Install

```bash
hermes plugins install worker-watch      # from the plugin catalog
hermes plugins enable worker-watch
```

or point Hermes at this checkout:

```bash
ln -s "$PWD/plugin/worker-watch" ~/.hermes/plugins/worker-watch
hermes plugins enable worker-watch
```

The plugin requires Hermes 0.15.0 or newer (`requires_hermes` in `plugin.yaml`).

## Tests

The suite runs from the repository root and needs `pytest`, `fastapi` and
`httpx` (the routes are exercised through FastAPI's TestClient); the bundle
harness additionally needs `node`.

```bash
python -m pytest
```

`tests/test_installed_layout.py` rebuilds the catalog install path from a clean
`git archive` of `plugin/worker-watch` and asserts the six routes answer while
the board and session stores stay byte-identical.

## License

MIT — see [LICENSE](LICENSE).
