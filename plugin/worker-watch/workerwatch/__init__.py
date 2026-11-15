"""Worker Watch read side — the package the plugin ships (ADR-0018).

Six modules, all stdlib-only, all reading existing stores through
``file:…?mode=ro`` URIs (I8): ``workerlink`` (card -> admitted chat-session
link), ``worker_watch`` (running cards + recent sessions + per-board graphs),
``task_tree`` (the ``task_links`` structure layer), ``root_chats`` (this host's
sessions and token counts from the session store), ``card_detail`` (one card,
fully, for the drawer) and ``card_restart`` (parked cards plus the sanctioned
restart through the owner's CLI).

This package is the whole install surface beside ``dashboard/``: a
plugin-catalog copy of ``plugin/worker-watch`` is self-contained. Nothing here
imports Hermes runtime modules (I7) or the enclosing repo; the repo reaches
these modules through the thin ``swetools/workerlink.py`` and
``swetools/root_chats.py`` re-export shims.
"""

__all__ = [
    "card_detail",
    "card_restart",
    "root_chats",
    "task_tree",
    "worker_watch",
    "workerlink",
]
