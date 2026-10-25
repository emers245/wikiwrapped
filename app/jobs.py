"""Run long work (sync, enrichment) in child processes, with visible progress.

The launcher opens its window within seconds and then syncs and enriches in
the background. Each step runs as a child process, not a thread, for four
reasons:

1. Isolation (D12). A sync that hangs on a Drive online-only file, or an
   enrichment that crashes inside numba or runs out of memory in torch,
   cannot freeze or kill the window. The dashboard keeps serving whatever
   data already exists.
2. It can be stopped. Python threads cannot be killed, so a stuck network
   read in a thread would keep the app alive after its window closed. A
   child process can be terminated on window close.
3. Memory comes back. The language model (hundreds of MB once torch is
   loaded) is released when the enrichment step exits, instead of living
   in the window's process for the rest of the session.
4. One implementation. The children are the same CLIs a developer runs by
   hand (python -m app.sync, scripts/enrich_pending.py), so there is no
   second pipeline to keep in step.

The cost is that progress has to cross the process boundary; app/progress.py
is that protocol. Every other line a child prints is forwarded to the app
log, which is how its output reaches the log file without the children
writing to it themselves (several processes rotating one log file is not
safe, especially on Windows).

One job at a time. A job is a sequence of steps, and a failing step does
not stop the ones after it: enrichment is worth running even when the sync
reported a problem, because it works on whatever is already in the
database. SQLite is shared safely between the children (writers) and the
API (readers) by WAL mode; see storage.connect.
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass, field

import config
from app import progress

log = logging.getLogger(__name__)

# How long a child gets to exit after a polite terminate before it is killed.
SHUTDOWN_GRACE_S = 5.0

# Lines of ordinary output kept per step, so a failure can say what the
# child printed last instead of only "exit status 1".
TAIL_LINES = 20


@dataclass
class Step:
    label: str        # what the window shows, e.g. "Syncing history"
    argv: list[str]


def sync_steps(force: bool = False, on_launch: bool = False,
               sync: bool = True, enrich: bool = True) -> list[Step]:
    """The standard job: sync every source, then enrich what is new.

    Shared by the launcher (on launch) and the API (a "sync now" request),
    so both always run the same commands.
    """
    python = sys.executable
    steps = []
    if sync:
        flags = ["--force"] if force else (["--on-launch"] if on_launch else [])
        steps.append(Step("Syncing history", [python, "-m", "app.sync", *flags]))
    if enrich:
        script = str(config.PROJECT_ROOT / "scripts" / "enrich_pending.py")
        steps.append(Step("Adding topics and map positions", [python, script]))
    return steps


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _State:
    id: int = 0
    job: str | None = None
    status: str = "idle"          # idle | running | done | failed | cancelled
    step: str | None = None
    step_index: int = 0
    steps: int = 0
    stage: str | None = None
    message: str = ""
    done: int | None = None
    total: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    errors: list[str] = field(default_factory=list)


class JobRunner:
    """Runs one job (a list of Steps) at a time in a background thread."""

    def __init__(self, cwd=None, env: dict | None = None):
        self._cwd = str(cwd or config.PROJECT_ROOT)
        self._env = env
        self._lock = threading.Lock()
        self._state = _State()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._cancelled = threading.Event()

    # -- public ------------------------------------------------------------

    def start(self, name: str, steps: list[Step]) -> bool:
        """Start a job; False (and no change) if one is already running."""
        with self._lock:
            if self._state.status == "running":
                return False
            if not steps:
                return False
            self._cancelled.clear()
            self._state = _State(
                id=self._state.id + 1, job=name, status="running",
                steps=len(steps), started_at=_now(),
            )
            self._thread = threading.Thread(
                target=self._run, args=(steps,), name=f"job-{name}", daemon=True
            )
            self._thread.start()
        log.info("Job %r started (%d step(s))", name, len(steps))
        return True

    def snapshot(self) -> dict:
        """A copy of the current state, safe to hand to the API."""
        with self._lock:
            state = self._state
            return {
                "id": state.id,
                "job": state.job,
                "status": state.status,
                "step": state.step,
                "step_index": state.step_index,
                "steps": state.steps,
                "stage": state.stage,
                "message": state.message,
                "done": state.done,
                "total": state.total,
                "started_at": state.started_at,
                "finished_at": state.finished_at,
                "errors": list(state.errors),
            }

    def running(self) -> bool:
        with self._lock:
            return self._state.status == "running"

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the current job finishes; True if it did in time."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def shutdown(self, timeout: float = SHUTDOWN_GRACE_S) -> None:
        """Stop the running job, if any: called when the window closes.

        Terminating mid-step is safe by design. The sync appends to the
        visit log with an fsync per batch and the log skips a torn final
        line (D9); enrichment commits per chunk and resumes from its caches.
        """
        self._cancelled.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            log.info("Stopping the running job (pid %s)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                log.warning("Job did not stop within %.0fs; killing it", timeout)
                proc.kill()
                proc.wait()
        self.wait(timeout)

    # -- the worker thread ---------------------------------------------------

    def _run(self, steps: list[Step]) -> None:
        failed = False
        for index, step in enumerate(steps, 1):
            if self._cancelled.is_set():
                break
            self._update(step=step.label, step_index=index, stage=None,
                         message=step.label, done=None, total=None)
            ok = self._run_step(step)
            failed = failed or not ok
        status = ("cancelled" if self._cancelled.is_set()
                  else "failed" if failed else "done")
        with self._lock:
            self._state.status = status
            self._state.finished_at = _now()
            self._state.done = self._state.total = None
            if status == "done":
                self._state.message = ""
            name = self._state.job
        log.info("Job %r finished: %s", name, status)

    def _run_step(self, step: Step) -> bool:
        env = dict(os.environ if self._env is None else self._env)
        env[progress.ENV_VAR] = "1"
        env["PYTHONUNBUFFERED"] = "1"           # progress lines arrive live
        env["PYTHONIOENCODING"] = "utf-8"
        # Windows: a child of a windowless app would otherwise flash a
        # console window for every step.
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        try:
            proc = subprocess.Popen(
                step.argv, cwd=self._cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1, creationflags=flags,
            )
        except OSError as exc:
            self._error(f"{step.label}: could not start ({exc})")
            return False

        self._proc = proc
        # shutdown() may have run between the cancel check in _run and the
        # Popen above, when there was no process yet to stop.
        if self._cancelled.is_set():
            proc.terminate()
        child_log = logging.getLogger("job")
        assert proc.stdout is not None
        with proc.stdout:                    # closes the pipe when the child ends
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                event = progress.parse(line)
                if event is not None:
                    self._update(
                        stage=event.get("stage"),
                        message=event.get("message") or step.label,
                        done=event.get("done"),
                        total=event.get("total"),
                    )
                elif line.strip():
                    tail.append(line)
                    child_log.info("[%s] %s", step.label, line)
        code = proc.wait()
        self._proc = None

        if code != 0 and not self._cancelled.is_set():
            detail = tail[-1] if tail else f"exit status {code}"
            self._error(f"{step.label} did not complete: {detail}")
            return False
        return True

    def _update(self, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(self._state, key, value)

    def _error(self, message: str) -> None:
        log.warning("%s", message)
        with self._lock:
            self._state.errors.append(message)
