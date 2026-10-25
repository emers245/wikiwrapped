"""Progress events from a child process to the launcher.

The launcher runs long work (sync, enrichment) as child processes so a hang
or crash cannot take the window down (see app/jobs.py). The cost of that
isolation is that progress has to cross a process boundary. This is the
whole protocol: the child prints one line per event to stdout,

    @@progress {"stage": "topics", "message": "...", "done": 12, "total": 340}

and the parent (jobs.JobRunner) recognizes the prefix, parses the JSON, and
logs every other line as ordinary output. Plain stdout was chosen over a
progress file or a socket because the pipe already exists (the parent
captures output for the log anyway), it needs no cleanup, and it cannot go
stale: when the child exits, the stream ends.

Events are emitted only when WIKIPEDIA_WRAPPED_PROGRESS=1, which the job
runner sets. Run from a terminal, the same code prints nothing extra and
the CLIs keep their tqdm bars.
"""

from __future__ import annotations

import json
import os
import sys

ENV_VAR = "WIKIPEDIA_WRAPPED_PROGRESS"
PREFIX = "@@progress "


def enabled() -> bool:
    return os.environ.get(ENV_VAR) == "1"


def emit(
    stage: str,
    message: str = "",
    done: int | None = None,
    total: int | None = None,
) -> None:
    """Report one step of progress; a no-op unless a job runner is listening.

    done/total are optional: a stage with no natural count (loading the
    language model, resolving titles) sends only a message, and the window
    shows it as indeterminate.
    """
    if not enabled():
        return
    event = {"stage": stage, "message": message, "done": done, "total": total}
    try:
        sys.stdout.write(PREFIX + json.dumps(event, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass  # a closed pipe must never break the work being reported on


def counter(stage: str, message: str):
    """A (done, total) callback that emits under a fixed stage and message.

    Handed to topics.label_titles / embeddings.embed_titles as on_progress,
    so those modules report counts without knowing this protocol exists.
    """
    def report(done: int, total: int) -> None:
        emit(stage, message, done, total)

    return report


def parse(line: str) -> dict | None:
    """The event in one output line, or None if it is not a progress line."""
    if not line.startswith(PREFIX):
        return None
    try:
        event = json.loads(line[len(PREFIX):])
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) else None
