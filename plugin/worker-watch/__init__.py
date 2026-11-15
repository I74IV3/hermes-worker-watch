"""Worker Watch — native plugin entry beside the dashboard tab (ADR-0025).

The plugin's whole surface is the dashboard: ``dashboard/manifest.json``
registers the tab and ``dashboard/plugin_api.py`` mounts one ``APIRouter`` at
``/api/plugins/worker-watch/``. Hermes' plugin loader only admits a directory
whose contents it can load (``__init__.py``, a ``desktop/plugin.js`` bundle or a
portable ``plugin.json``), so the plugin root carries this module beside the
dashboard subdir. :func:`register` registers nothing on purpose — no tools, no
hooks, no middleware — and ``tests/test_worker_watch_plugin.py`` pins that
against a recording context, so an undeclared capability cannot appear here
unnoticed.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Register no capability: the plugin is its dashboard router and its tab."""
    del ctx
