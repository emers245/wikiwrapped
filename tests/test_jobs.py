"""app.jobs + app.progress: background steps as child processes.

The children here are tiny Python scripts written to tmp_path, so the
runner is exercised for real (process start, output parsing, exit codes,
termination) without running an actual sync."""

import logging
import os
import sys
import textwrap
import time

import pytest

import config
from app import jobs, progress

ROOT = str(config.PROJECT_ROOT)


def script(tmp_path, name, body):
    """A child script that can import the app (for app.progress)."""
    path = tmp_path / name
    path.write_text(
        f"import sys\nsys.path.insert(0, {ROOT!r})\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    return jobs.Step(name, [sys.executable, str(path)])


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def runner(tmp_path):
    job_runner = jobs.JobRunner(cwd=tmp_path)
    yield job_runner
    job_runner.shutdown(timeout=2)


# ---------------------------------------------------------------------------
# The progress protocol
# ---------------------------------------------------------------------------

def test_emit_is_silent_unless_a_runner_is_listening(monkeypatch, capsys):
    monkeypatch.delenv(progress.ENV_VAR, raising=False)
    progress.emit("topics", "Labeling", 1, 2)
    assert capsys.readouterr().out == ""


def test_emit_and_parse_roundtrip(monkeypatch, capsys):
    monkeypatch.setenv(progress.ENV_VAR, "1")
    progress.counter("topics", "Labeling")(3, 10)
    line = capsys.readouterr().out.strip()
    assert progress.parse(line) == {
        "stage": "topics", "message": "Labeling", "done": 3, "total": 10
    }


def test_ordinary_output_is_not_mistaken_for_progress():
    assert progress.parse("Local Chrome: 5 new visit(s) logged") is None
    assert progress.parse("@@progress {not json") is None


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def test_progress_reaches_the_snapshot_while_the_step_runs(runner, tmp_path):
    step = script(tmp_path, "child.py", f"""
        import pathlib, time
        from app import progress
        progress.emit("topics", "Labeling article topics", 4, 9)
        while not pathlib.Path({str(tmp_path / 'go')!r}).exists():
            time.sleep(0.02)
    """)
    assert runner.start("test", [step])
    assert wait_until(lambda: runner.snapshot()["done"] == 4)
    snap = runner.snapshot()
    assert snap["status"] == "running"
    assert snap["message"] == "Labeling article topics"
    assert (snap["stage"], snap["total"]) == ("topics", 9)

    (tmp_path / "go").write_text("", encoding="utf-8")
    assert runner.wait(10)
    assert runner.snapshot()["status"] == "done"


def test_steps_run_in_order_and_a_failure_does_not_stop_the_next(runner, tmp_path):
    marker = tmp_path / "second-ran"
    first = script(tmp_path, "first.py", """
        print("something went wrong: boom")
        sys.exit(3)
    """)
    second = script(tmp_path, "second.py", f"""
        open({str(marker)!r}, "w").close()
    """)
    assert runner.start("test", [first, second])
    assert runner.wait(10)

    snap = runner.snapshot()
    assert marker.exists()                       # enrichment still runs
    assert snap["status"] == "failed"
    assert snap["steps"] == 2 and snap["step_index"] == 2
    assert any("boom" in e for e in snap["errors"])


def test_child_output_is_logged(runner, tmp_path, caplog):
    step = script(tmp_path, "talk.py", 'print("Database: +2 visit(s)")\n')
    with caplog.at_level(logging.INFO, logger="job"):
        runner.start("test", [step])
        runner.wait(10)
    assert any("Database: +2 visit(s)" in r.getMessage() for r in caplog.records)


def test_only_one_job_at_a_time(runner, tmp_path):
    slow = script(tmp_path, "slow.py", "import time\ntime.sleep(30)\n")
    assert runner.start("first", [slow])
    assert runner.start("second", [slow]) is False
    assert runner.snapshot()["job"] == "first"


def test_shutdown_stops_a_stuck_child_promptly(runner, tmp_path):
    pid_file = tmp_path / "pid"
    stuck = script(tmp_path, "stuck.py", f"""
        import os, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        time.sleep(600)
    """)
    runner.start("test", [stuck, stuck])
    assert wait_until(pid_file.exists)
    pid = int(pid_file.read_text())

    started = time.monotonic()
    runner.shutdown(timeout=2)
    assert time.monotonic() - started < 8
    assert runner.snapshot()["status"] == "cancelled"
    assert runner.snapshot()["step_index"] == 1     # the second never started
    assert not _alive(pid)


def test_children_are_told_to_report_progress(runner, tmp_path):
    seen = tmp_path / "env"
    step = script(tmp_path, "env.py", f"""
        import os
        open({str(seen)!r}, "w").write(os.environ.get({progress.ENV_VAR!r}, ""))
    """)
    runner.start("test", [step])
    runner.wait(10)
    assert seen.read_text() == "1"


def test_a_missing_program_is_a_recorded_failure(runner, tmp_path):
    runner.start("test", [jobs.Step("Nope", [str(tmp_path / "no-such-program")])])
    runner.wait(10)
    snap = runner.snapshot()
    assert snap["status"] == "failed"
    assert "could not start" in snap["errors"][0]


def test_job_ids_increase_so_the_window_can_tell_jobs_apart(runner, tmp_path):
    quick = script(tmp_path, "quick.py", "pass\n")
    runner.start("a", [quick])
    runner.wait(10)
    first = runner.snapshot()["id"]
    runner.start("b", [quick])
    runner.wait(10)
    assert runner.snapshot()["id"] == first + 1


def test_sync_steps_compose_the_standard_job():
    steps = jobs.sync_steps(on_launch=True)
    assert [s.label for s in steps] == ["Syncing history",
                                        "Adding topics and map positions"]
    assert steps[0].argv[1:] == ["-m", "app.sync", "--on-launch"]
    assert steps[1].argv[-1].endswith("enrich_pending.py")
    assert jobs.sync_steps(force=True)[0].argv[-1] == "--force"
    assert [s.label for s in jobs.sync_steps(sync=False)] == [
        "Adding topics and map positions"]
    assert jobs.sync_steps(sync=False, enrich=False) == []


def _alive(pid):
    if os.name == "nt":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    # The runner waits on (reaps) its own children, so a stopped child is
    # gone from the process table, not left as a zombie.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
