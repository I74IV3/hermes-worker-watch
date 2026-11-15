"""Contract tests for workerwatch.card_restart — the parked-card read side and the
one write the Worker Watch tab is allowed (ADR-0013).

Two halves, both pinned here:

* ``stalled_snapshot`` — which cards count as parked (``ready``, assigned, still
  carrying a ``last_failure_error``), read through the read-only URI.
* ``restart_card`` — the refusal set (no direct DB write, ever) and the one
  write path the repo sanctions: the CLI's own ``block`` + ``unblock`` pair, with
  a post-condition check so a CLI that answers 0 without clearing the stamp is a
  failure, not a success.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from workerwatch.card_restart import restart_card, retry_available, stalled_snapshot
from workerwatch.worker_watch import connect_ro

SCHEMA = """
CREATE TABLE tasks (
    id                  TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    assignee            TEXT,
    status              TEXT NOT NULL,
    started_at          INTEGER,
    current_run_id      INTEGER,
    claim_lock          TEXT,
    last_failure_error  TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);
"""

QUOTA = "pid 2222290 exited rate-limited (quota wall) — requeued without counting a failure"


def _board(home: Path, slug: str, tasks=()) -> Path:
    db = home / "kanban" / "boards" / slug / "kanban.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany(
        "insert into tasks (id, title, assignee, status, started_at, current_run_id,"
        " claim_lock, last_failure_error) values (?,?,?,?,?,?,?,?)",
        tasks,
    )
    con.commit()
    con.close()
    return db


def _fixture(home: Path) -> Path:
    db = _board(
        home,
        "lora",
        tasks=[
            ("t_parked", "parked card", "default", "ready", 100, None, None, QUOTA),
            ("t_clean", "clean ready card", "default", "ready", 101, None, None, None),
            ("t_unassigned", "owner decision", None, "ready", 102, None, None, QUOTA),
            ("t_running", "running card", "default", "running", 103, 7, "lock", QUOTA),
            ("t_done", "done card", "default", "done", 104, None, None, QUOTA),
        ],
    )
    _board(
        home,
        "renderpool",
        tasks=[("t_ok", "no stamp here", "default", "ready", 1, None, None, None)],
    )
    bad = home / "kanban" / "boards" / "broken" / "kanban.db"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not a database")
    return db


# ---------------------------------------------------------------- read side


def test_stalled_lists_only_ready_assigned_stamped_cards(tmp_path: Path) -> None:
    _fixture(tmp_path)
    snap = stalled_snapshot(tmp_path)
    assert snap["count"] == 1
    assert [(b["board"], [r["id"] for r in b["rows"]]) for b in snap["boards"]] == [
        ("lora", ["t_parked"])
    ]
    row = snap["boards"][0]["rows"][0]
    assert row["title"] == "parked card"
    assert row["assignee"] == "default"
    assert row["status"] == "ready"
    assert row["error"] == QUOTA


def test_stalled_board_filter_narrows_and_empty_boards_vanish(tmp_path: Path) -> None:
    _fixture(tmp_path)
    assert stalled_snapshot(tmp_path, board="renderpool") == {"boards": [], "count": 0}
    snap = stalled_snapshot(tmp_path, board="lora")
    assert [b["board"] for b in snap["boards"]] == ["lora"]


def test_stalled_skips_an_unopenable_board(tmp_path: Path) -> None:
    _fixture(tmp_path)
    snap = stalled_snapshot(tmp_path)
    assert [b["board"] for b in snap["boards"]] == ["lora"]


def test_stalled_reads_through_the_read_only_uri(tmp_path: Path) -> None:
    db = _board(tmp_path, "lora", tasks=[("t_p", "p", "default", "ready", 1, None, None, QUOTA)])
    con = connect_ro(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("update tasks set status = 'x'")
    finally:
        con.close()


# --------------------------------------------------------------- write side


class Runner:
    """Records argv and answers like the CLI; ``effect`` runs on the retry leg."""

    def __init__(self, effect=None, rc=0, stderr=""):
        self.calls: list[list[str]] = []
        self.effect = effect
        self.rc = rc
        self.stderr = stderr

    def __call__(self, argv, **kwargs):  # noqa: ANN001 - mirrors subprocess.run
        self.calls.append(list(argv))
        if self.effect is not None and "retry" in argv:
            self.effect()
        return type("Done", (), {"returncode": self.rc, "stdout": "", "stderr": self.stderr})()


def _clear(db: Path):
    def effect():
        con = sqlite3.connect(db)
        con.execute(
            "update tasks set status='ready', current_run_id=NULL,"
            " consecutive_failures=0, last_failure_error=NULL where id='t_parked'"
        )
        con.commit()
        con.close()

    return effect


def _park(db: Path) -> None:
    con = sqlite3.connect(db)
    con.execute(
        "update tasks set status='ready', claim_lock=NULL, current_run_id=NULL,"
        " last_failure_error=? where id='t_parked'",
        (QUOTA,),
    )
    con.commit()
    con.close()


def test_restart_refuses_an_unknown_card(tmp_path: Path) -> None:
    _fixture(tmp_path)
    runner = Runner()
    out = restart_card("lora", "t_nope", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 404)
    assert runner.calls == []


def test_restart_refuses_an_unassigned_card(tmp_path: Path) -> None:
    _fixture(tmp_path)
    runner = Runner()
    out = restart_card("lora", "t_unassigned", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 409)
    assert "assign" in out["reason"]
    assert runner.calls == []


def test_restart_refuses_a_card_that_is_not_ready(tmp_path: Path) -> None:
    _fixture(tmp_path)
    runner = Runner()
    out = restart_card("lora", "t_done", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 409)
    assert "done" in out["reason"]
    assert runner.calls == []


def test_restart_refuses_a_card_with_a_live_run(tmp_path: Path) -> None:
    _fixture(tmp_path)
    _board(tmp_path, "lora2", tasks=[("t_live", "live", "default", "ready", 1, 9, "lock", QUOTA)])
    runner = Runner()
    out = restart_card("lora2", "t_live", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 409)
    assert "run is active" in out["reason"]
    assert runner.calls == []


def test_restart_refuses_when_there_is_no_stamp_to_clear(tmp_path: Path) -> None:
    _fixture(tmp_path)
    runner = Runner()
    out = restart_card("lora", "t_clean", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 409)
    assert "no failure stamp" in out["reason"]
    assert runner.calls == []


def test_restart_refuses_a_stamp_that_is_not_quota_flavoured(tmp_path: Path) -> None:
    db = _board(
        tmp_path,
        "lora3",
        tasks=[
            ("t_crash", "crashed", "default", "ready", 1, None, None, "Traceback: KeyError 'x'")
        ],
    )
    runner = Runner()
    out = restart_card("lora3", "t_crash", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 409)
    assert "not a quota" in out["reason"]
    assert runner.calls == []
    con = sqlite3.connect(db)
    kept = con.execute("select last_failure_error from tasks where id='t_crash'").fetchone()[0]
    con.close()
    assert kept.startswith("Traceback")


@pytest.mark.parametrize(
    "board,task", [("../etc", "t_parked"), ("lora", "../../x"), ("", "t_parked")]
)
def test_restart_refuses_unsafe_identifiers(tmp_path: Path, board: str, task: str) -> None:
    _fixture(tmp_path)
    runner = Runner()
    out = restart_card(board, task, home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 400)
    assert runner.calls == []


def test_restart_retries_once_and_confirms_the_clear(tmp_path: Path) -> None:
    db = _fixture(tmp_path)
    runner = Runner(effect=_clear(db))
    out = restart_card("lora", "t_parked", home=tmp_path, runner=runner)
    # one leg, not a block/unblock pair: the pair trips the block-loop breaker on
    # its second use (triage + surviving stamp), which is the defect this verb fixes
    assert runner.calls == [["hermes", "kanban", "--board", "lora", "retry", "t_parked"]]
    assert out["ok"] is True
    assert out["status"] == "ready"
    assert out["cleared_error"] == QUOTA
    assert out["steps"] == ["retry"]


def test_restart_reports_a_cli_failure_without_claiming_success(tmp_path: Path) -> None:
    _fixture(tmp_path)
    runner = Runner(rc=1, stderr="cannot unblock (not blocked/scheduled?)")
    out = restart_card("lora", "t_parked", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 502)
    assert "not blocked" in out["reason"]


def test_restart_reports_a_stamp_that_survived_the_cli(tmp_path: Path) -> None:
    _fixture(tmp_path)
    runner = Runner()  # answers 0 but changes nothing
    out = restart_card("lora", "t_parked", home=tmp_path, runner=runner)
    assert (out["ok"], out["status"]) == (False, 502)
    assert "survived" in out["reason"]


# ---------------------------------------------------------- capability probe


def test_retry_available_when_the_verb_answers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_BIN", raising=False)
    runner = Runner()
    assert retry_available(runner) is True
    assert runner.calls == [["hermes", "kanban", "retry", "--help"]]


def test_retry_available_is_false_on_an_unknown_verb() -> None:
    """A stock Hermes answers argparse rc=2 for a verb it never shipped
    (upstream #149556 is open); that is the disarm signal, not an error."""
    runner = Runner(rc=2, stderr="usage: hermes kanban [-h] ... invalid choice: 'retry'")
    assert retry_available(runner) is False


def test_retry_available_is_false_when_the_cli_is_missing() -> None:
    def missing(argv, **kwargs):  # noqa: ANN001, ANN003 - mirrors subprocess.run
        raise FileNotFoundError("hermes not on PATH")

    assert retry_available(missing) is False


def test_retry_available_is_false_when_the_probe_times_out() -> None:
    def hang(argv, **kwargs):  # noqa: ANN001, ANN003 - mirrors subprocess.run
        raise subprocess.TimeoutExpired(cmd=argv, timeout=10)

    assert retry_available(hang) is False
