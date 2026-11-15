"""The install path a plugin-catalog entry takes, proven from a clean checkout.

A catalog install clones the entry's repo, checks out the pinned SHA and copies
only that subdir under ``<hermes_home>/plugins/<name>``. The earlier pins prove
the *working tree* is self-contained (``test_a_catalog_copy_mounts_without_the_repo``
copies the checkout dir); this module proves the **tracked** tree is, which is
the only tree an install ever sees: the copy comes from ``git archive`` of the
subdir, so an install file that is untracked, gitignored or generated at test
time vanishes here exactly as it would in the catalog.

The probe runs in a fresh interpreter whose cwd and ``sys.path`` hold no
devtools repo, mounts the copied ``plugin_api.py`` through FastAPI, and then
asserts three things this card exists for:

* the five GET routes answer 200 with the fixture boards' facts, and
  ``POST /restart`` follows the installed capability probe — the degraded shape
  (no ``kanban retry`` verb: 502, stamp intact) and the armed shape (the verb
  clears the stamp: 200, stamp gone);
* the board DBs and the session store are byte-identical after the reads, with
  no ``-wal``/``-shm`` sidecar anywhere under the home;
* ``workerwatch`` resolves from the installed copy, never from a repo.

``hermes plugins validate`` against that same checkout is a hand-run leg: the
suite never calls the hermes CLI (ADR-0001).
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

try:  # the devtools checkout carries the shared helper; the plugin's own repo does not
    from swetools.scaffold import clean_git_env
except ImportError:  # pragma: no cover - exercised only in the standalone plugin repo
    _GIT_ENV_REDIRECTS = (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    )

    def clean_git_env(env: dict[str, str] | None = None) -> dict[str, str]:
        """*env* (default: the process environment) with every git redirect removed."""
        src = os.environ if env is None else env
        return {k: v for k, v in src.items() if k not in _GIT_ENV_REDIRECTS}


HERE = Path(__file__).resolve().parent  # plugin/worker-watch/tests
REPO = HERE.parents[2]  # the repository root (devtools now; the plugin's own repo)
SUBDIR = "plugin/worker-watch"

# The files an install cannot live without: the installer manifest the loader
# reads, the native entry it loads, the tab manifest, the mounted router and the
# package beside it. A catalog checkout that misses one is a dead plugin.
INSTALL_FILES = (
    "plugin.yaml",
    "__init__.py",
    "dashboard/manifest.json",
    "dashboard/plugin_api.py",
    "dashboard/dist/index.js",
    "workerwatch/__init__.py",
    "workerwatch/worker_watch.py",
    "workerwatch/task_tree.py",
    "workerwatch/root_chats.py",
    "workerwatch/card_detail.py",
    "workerwatch/card_restart.py",
    "workerwatch/workerlink.py",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    env = clean_git_env()
    # a repo on the import path would hide the very dependency this probes
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [git, *args], cwd=REPO, capture_output=True, text=True, env=env, check=False
    )


def _git_bytes(*args: str) -> subprocess.CompletedProcess[bytes]:
    """A git call whose stdout is binary (``archive``): text mode would eat CR bytes."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    env = clean_git_env()
    env.pop("PYTHONPATH", None)
    return subprocess.run([git, *args], cwd=REPO, capture_output=True, env=env, check=False)


def _archive(dest: Path) -> Path:
    """Extract ``HEAD:plugin/worker-watch`` into *dest*: the tracked tree alone."""
    proc = _git_bytes("archive", "--format=tar", f"HEAD:{SUBDIR}")
    assert proc.returncode == 0, proc.stderr
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(dest, filter="data")
        else:  # pragma: no cover - the host runs 3.14
            tar.extractall(dest)
    return dest


def _installed_home(tmp_path: Path) -> Path:
    """A throwaway HERMES_HOME carrying the checkout under ``plugins/``."""
    home = tmp_path / "home"
    plugins = home / "plugins"
    plugins.mkdir(parents=True)
    _archive(plugins / "worker-watch")
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - worker-watch\n", encoding="utf-8"
    )
    return home


def _run_probe(home: Path) -> subprocess.CompletedProcess[str]:
    """Run the probe in a fresh interpreter, with no repo root on its import path.

    Deliberately NOT ``uv run``: uv puts the project root on ``sys.path``, which
    would let a plugin module that resolves back into devtools keep working and
    hide exactly the dependency this probe exists to find. ``sys.executable`` is
    this venv's interpreter — the one the gate runs under — and the probe needs
    no project install, only fastapi from it.
    """
    probe = home.parent / "install_probe.py"
    probe.write_text(_PROBE, encoding="utf-8")
    env = clean_git_env()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(probe), str(home), str(REPO)],
        cwd=home.parent,
        capture_output=True,
        text=True,
        timeout=300,
        env=env,
        check=False,
    )


def test_the_install_files_are_tracked_in_git() -> None:
    """A gitignored install file passes every working-tree test and ships empty.

    ``git archive`` is what a catalog checkout produces, so each file the plugin
    cannot load without must be tracked at HEAD.
    """
    for rel in INSTALL_FILES:
        assert (_git("ls-files", "--error-unmatch", f"{SUBDIR}/{rel}").returncode) == 0, rel


def test_a_clean_checkout_survives_the_install_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The card's E2E: tracked tree -> plugins dir -> six routes -> untouched DBs."""
    home = _installed_home(tmp_path)
    installed = home / "plugins" / "worker-watch"
    for rel in INSTALL_FILES:
        assert (installed / rel).is_file(), rel
    # the checkout is not inside a repo: nothing resolves back to devtools
    assert _git_here(installed) is None, "the checkout sits inside a git repo"

    proc = _run_probe(home)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "INSTALL-OK" in proc.stdout, out
    assert proc.stdout.splitlines()[-1] == "INSTALL-OK"
    # the facts the probe printed, in order: the fixture boards, the chat row,
    # the parked card, both restart states, and the byte-identical DB set
    facts = [line for line in proc.stdout.splitlines() if line.startswith("fact ")]
    assert facts == [
        "fact boards devtools,renderpool",
        "fact running devtools:t_run,renderpool:t_run",
        "fact chats s-human",
        "fact stalled devtools:t_parked",
        "fact restart-degraded 502 retry failed (rc=2)",
        "fact readonly boards=2 db-set=3 sidecars=0",
        "fact restart-available 200 ready",
    ]


def test_the_probe_bites_when_a_read_module_is_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A checkout without its store read must fail the probe, not pass silently."""
    home = _installed_home(tmp_path)
    (home / "plugins" / "worker-watch" / "workerwatch" / "root_chats.py").unlink()
    proc = _run_probe(home)
    assert proc.returncode != 0
    assert "INSTALL-OK" not in proc.stdout
    assert "root_chats.py" in proc.stdout + proc.stderr


def test_the_read_only_assertion_bites_on_a_writable_read(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A read path that opens the board read-write must redden the byte check.

    The probe's read-only leg is the one claim a passing 200 cannot carry, so it
    gets its own negative control: the installed module is patched to open the
    board ``mode=rw`` and stamp ``PRAGMA user_version`` (a real byte change in the
    DB header), and the probe must fail on the changed hash.
    """
    home = _installed_home(tmp_path)
    module = home / "plugins" / "worker-watch" / "workerwatch" / "worker_watch.py"
    source = module.read_text(encoding="utf-8")
    victim = 'return sqlite3.connect(f"file:{db}?mode=ro", uri=True)'
    assert victim in source
    patched = source.replace(
        victim,
        'con = sqlite3.connect(f"file:{db}?mode=rw", uri=True)\n'
        '    con.execute("pragma user_version = 7")\n'
        "    con.commit()\n"
        "    return con",
    )
    module.write_text(patched, encoding="utf-8")
    proc = _run_probe(home)
    assert proc.returncode != 0
    assert "READONLY-FAIL" in proc.stdout + proc.stderr
    assert "INSTALL-OK" not in proc.stdout


def _git_here(path: Path) -> str | None:
    """The toplevel of the repo containing *path*, or ``None``."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not on PATH")
    env = clean_git_env()
    env.pop("PYTHONPATH", None)
    proc = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        cwd=path,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


# The probe: one fresh interpreter, no devtools repo anywhere on its path. It
# builds its own fixtures (two boards, a parked card, a session store), generates
# the two capability stubs for the restart legs, mounts the *installed* router
# and prints the facts the test above pins.
_PROBE = '''
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

home = Path(sys.argv[1]).resolve()
repo = Path(sys.argv[2]).resolve()
plugins = home / "plugins" / "worker-watch"
if Path.cwd().resolve() != home.parent:
    print(f"PROBE-FAIL cwd is {Path.cwd()}, expected {home.parent}")
    raise SystemExit(1)
# This venv carries devtools as an editable install, so its root sits on
# sys.path before the probe runs. Drop it: a plugin module that resolved back
# into the repo would otherwise pass here and die on a catalog install.
dropped = [p for p in sys.path if p and Path(p).resolve() == repo]
sys.path[:] = [p for p in sys.path if p not in dropped]
if dropped:
    print(f"note dropped {len(dropped)} devtools path(s) from sys.path")
if any(p and Path(p).resolve() == repo for p in sys.path):
    print("PROBE-FAIL the devtools repo is still on sys.path")
    raise SystemExit(1)
if str(plugins) in sys.path:
    print("PROBE-FAIL the copy is already on sys.path")
    raise SystemExit(1)

TASK_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, current_run_id INTEGER, started_at INTEGER,
    claim_lock TEXT, block_kind TEXT, last_failure_error TEXT, result TEXT,
    created_at INTEGER, completed_at INTEGER, branch_name TEXT, workspace_path TEXT
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT, started_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT, created_at INTEGER NOT NULL
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, author TEXT, body TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_links (
    parent_id TEXT NOT NULL, child_id TEXT NOT NULL, PRIMARY KEY (parent_id, child_id)
);
"""
SESSIONS = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, parent_session_id TEXT, title TEXT,
    started_at REAL, last_activity_at REAL, archived INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT, model TEXT, message_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count INTEGER NOT NULL DEFAULT 0, last_activity_description TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0
);
"""
QUOTA = "pid 4242 exited rate-limited (quota wall) - requeued without counting a failure"


def build_board(slug, tasks):
    db = home / "kanban" / "boards" / slug / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(TASK_SCHEMA)
    con.executemany(
        "insert into tasks (id, title, body, assignee, status, current_run_id, started_at,"
        " last_failure_error, created_at) values (?,?,?,?,?,?,?,?,?)",
        tasks,
    )
    con.commit()
    con.close()
    return db


build_board(
    "devtools",
    [
        ("t_run", "running card", "package it", "default", "running", 7, 100, None, 90),
        ("t_done", "finished card", "land the manifest", "default", "done", None, 80, None, 70),
        ("t_parked", "parked card", None, "default", "ready", None, 60, QUOTA, 50),
    ],
)
con = sqlite3.connect(home / "kanban" / "boards" / "devtools" / "kanban.db")
con.execute(
    "insert into task_runs (id, task_id, profile, started_at) values (7,'t_run','default',200)"
)
con.execute(
    "insert into task_events (task_id, kind, payload, created_at)"
    " values ('t_run','owner_admitted',?,200)",
    (json.dumps({"session_id": "sess-run"}),),
)
con.execute("insert into task_links (parent_id, child_id) values ('t_done','t_run')")
con.commit()
con.close()
build_board(
    "renderpool", [("t_run", "running card", None, "default", "running", None, 50, None, 40)]
)

con = sqlite3.connect(home / "state.db")
con.executescript(SESSIONS)
con.executemany(
    "insert into sessions (id, source, title, started_at, last_activity_at, pinned,"
    " profile_name, model, input_tokens, output_tokens) values (?,?,?,?,?,?,?,?,?,?)",
    [
        ("s-human", "tui", "Review the packaging proof", 10.0, 900.0, 1, "default",
         "qwen-max", 5000, 2500),
        ("sess-run", "kanban", "worker run", 100.0, 200.0, 0, "default", "qwen-max",
         210000, 105000),
    ],
)
con.commit()
con.close()


def stub(name, body):
    path = home.parent / name
    path.write_text("#!/usr/bin/env python3\\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# degraded: this host's real shape - the CLI has no `kanban retry` verb at all
degraded = stub(
    "hermes-degraded",
    "import sys\\n"
    "print(\\"hermes kanban: 'retry' is not a `hermes kanban` command.\\", file=sys.stderr)\\n"
    "raise SystemExit(2)\\n",
)
# armed: the verb answers and does what the owner's own retry does - clears the stamp
available = stub(
    "hermes-available",
    "import os, sqlite3, sys\\n"
    "argv = sys.argv[1:]\\n"
    "if '--help' in argv:\\n"
    "    raise SystemExit(0)\\n"
    "board = argv[argv.index('--board') + 1]\\n"
    "task_id = argv[-1]\\n"
    "db = os.path.join(os.environ['HERMES_HOME'], 'kanban', 'boards', board, 'kanban.db')\\n"
    "con = sqlite3.connect(db)\\n"
    "con.execute('update tasks set last_failure_error = null where id = ?', (task_id,))\\n"
    "con.commit()\\n"
    "con.close()\\n",
)

os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_BIN"] = degraded

try:
    import workerwatch  # noqa: F401 - must NOT resolve before the plugin loads

    print(f"PROBE-FAIL workerwatch resolved from {workerwatch.__file__} before the plugin loaded")
    raise SystemExit(1)
except ImportError:
    pass

spec = importlib.util.spec_from_file_location(
    "installed_api", plugins / "dashboard" / "plugin_api.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import workerwatch.worker_watch as ww  # noqa: E402

resolved = Path(ww.__file__).resolve()
if plugins.resolve() not in resolved.parents:
    print(f"PROBE-FAIL workerwatch loaded from {resolved}, not the installed copy")
    raise SystemExit(1)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

app = FastAPI()
app.include_router(mod.router, prefix="/api/plugins/worker-watch")
client = TestClient(app)
base = "/api/plugins/worker-watch"


def db_state():
    """(sha256 -> path) of every store DB, plus the sidecar files under the home."""
    hashes = {}
    for path in sorted(home.rglob("*.db")):
        hashes[str(path.relative_to(home))] = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecars = sorted(str(p.relative_to(home)) for p in home.rglob("*-wal")) + sorted(
        str(p.relative_to(home)) for p in home.rglob("*-shm")
    )
    return hashes, sidecars


before, before_side = db_state()

paths = ("/watch", "/graph", "/chats", "/card?board=devtools&task=t_run", "/stalled")
statuses = {p: client.get(base + p).status_code for p in paths}
bad = {p: c for p, c in statuses.items() if c != 200}
if bad:
    print(f"PROBE-FAIL GET statuses {bad}")
    raise SystemExit(1)

watch = client.get(base + "/watch").json()
print("fact boards " + ",".join(g["board"] for g in watch["boards"]))
print(
    "fact running "
    + ",".join(f"{g['board']}:{r['task_id']}" for g in watch["boards"] for r in g["running"])
)
chats = client.get(base + "/chats").json()
print("fact chats " + ",".join(r["session_id"] for r in chats["chats"]))
stalled = client.get(base + "/stalled").json()
print(
    "fact stalled "
    + ",".join(f"{b['board']}:{r['id']}" for b in stalled["boards"] for r in b["rows"])
)
if stalled["retry_available"] is not False:
    print(f"PROBE-FAIL retry_available is {stalled['retry_available']!r} on a stock install")
    raise SystemExit(1)

# the degraded write: refused, and the row keeps its stamp
refused = client.post(base + "/restart", json={"board": "devtools", "task_id": "t_parked"})
detail = refused.json().get("detail", "")
print(f"fact restart-degraded {refused.status_code} {detail.split(':')[0]}")
if refused.status_code != 502 or "rc=2" not in detail:
    print(f"PROBE-FAIL degraded restart answered {refused.status_code} {detail!r}")
    raise SystemExit(1)
row = sqlite3.connect(f"file:{home}/kanban/boards/devtools/kanban.db?mode=ro", uri=True)
stamp = row.execute("select last_failure_error from tasks where id='t_parked'").fetchone()[0]
row.close()
if not stamp:
    print("PROBE-FAIL the refused restart still cleared the stamp")
    raise SystemExit(1)

after_reads, after_side = db_state()
if after_reads != before or before_side or after_side:
    changed = {k: (before.get(k), after_reads.get(k)) for k in set(before) | set(after_reads)
               if before.get(k) != after_reads.get(k)}
    print(f"READONLY-FAIL changed={changed} sidecars={before_side + after_side}")
    raise SystemExit(1)
print(f"fact readonly boards=2 db-set={len(before)} sidecars=0")

# the armed write, from the same installed router: flip the probe and the CLI
os.environ["HERMES_BIN"] = available
stalled = client.get(base + "/stalled").json()
if stalled["retry_available"] is not True:
    print(f"PROBE-FAIL the armed stub still probes unavailable: {stalled['retry_available']}")
    raise SystemExit(1)
ok = client.post(base + "/restart", json={"board": "devtools", "task_id": "t_parked"})
body = ok.json()
print(f"fact restart-available {ok.status_code} {body.get('status')}")
if ok.status_code != 200 or body.get("ok") is not True:
    print(f"PROBE-FAIL armed restart answered {ok.status_code} {body}")
    raise SystemExit(1)
row = sqlite3.connect(f"file:{home}/kanban/boards/devtools/kanban.db?mode=ro", uri=True)
stamp = row.execute("select last_failure_error from tasks where id='t_parked'").fetchone()[0]
row.close()
if stamp:
    print("PROBE-FAIL the armed restart left the stamp in place")
    raise SystemExit(1)

print("INSTALL-OK")
'''
