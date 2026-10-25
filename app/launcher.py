"""Start Wikipedia Wrapped: server, background sync, and the app window.

    python -m app.launcher                 the app window (pywebview)
    python -m app.launcher --browser       a normal browser tab instead
    python -m app.launcher --no-sync       skip the check-on-launch sync
    python -m app.launcher --force-sync    sync even if one ran recently
    python -m app.launcher --no-enrich     skip topic/map enrichment
    python -m app.launcher --port 8123     prefer a specific port
    python -m app.launcher --self-test     check the installation, headless

What happens, in order, and why:

1. Logging to a rotating file in the user-data dir, from the first line.
   An installed app has no console, so the log file is the only place a
   startup problem can ever be read.
2. If this user's server is already running (a window left open), reuse
   it: open another window on it instead of starting a second server
   against the same database.
3. Start uvicorn on 127.0.0.1 only, in a background thread. pywebview has
   to own the main thread (macOS only allows UI on it), so the server
   cannot. The port is a fixed preferred one when free, a random free one
   otherwise; see choose_socket for why it is not always random.
4. Open the window as soon as the server answers, typically well under a
   second. The sync and enrichment run AFTER that, as background jobs
   (app/jobs.py) whose progress the window shows; they never block it.
5. When the window closes: stop the job (terminating its child process),
   stop the server, remove server.json, exit.

pywebview is imported only inside run_window(), so the API, the jobs, and
the test suite never need it (and --browser works without it).
"""

from __future__ import annotations

import argparse
import errno
import json
import logging
import logging.handlers
import os
import platform
import signal
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import config
from app import jobs, migrate, setup_service

log = logging.getLogger("launcher")

HOST = "127.0.0.1"

# Tried first on every launch. A stable port means a stable origin
# (http://127.0.0.1:48213), and the browser engine keys localStorage by
# origin: with a random port every launch, the remembered panel width and
# stow state (main.js) would be forgotten each time. Any free port is used
# when this one is taken.
PREFERRED_PORT = 48213

SERVER_RECORD = "server.json"
LOG_FILE = "app.log"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5

WINDOW_TITLE = "Wikipedia Wrapped"
WINDOW_SIZE = (1280, 820)
WINDOW_MIN_SIZE = (720, 480)
WINDOW_BACKGROUND = "#faf9f6"   # style.css --bg, so the first paint matches

READY_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_dir: Path | None = None) -> Path:
    """Rotating file log (always) plus stderr (when there is one).

    Only this process writes the file. The job children's output reaches it
    through a pipe (app/jobs.py), because several processes rotating one
    file is unsafe, and on Windows a rename of an open file fails outright.
    """
    log_dir = Path(log_dir or config.LOG_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_FILE
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    # A windowed app on Windows (pythonw) has no stderr at all.
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root.addHandler(console)
    logging.captureWarnings(True)

    def log_uncaught(kind, value, tb):
        log.critical("Uncaught exception", exc_info=(kind, value, tb))

    sys.excepthook = log_uncaught
    return path


# ---------------------------------------------------------------------------
# Is this user's server already running?
# ---------------------------------------------------------------------------

def server_record_path() -> Path:
    return Path(config.USER_DATA_DIR) / SERVER_RECORD


def write_server_record(port: int) -> None:
    path = server_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "port": port,
        "pid": os.getpid(),
        "version": config.APP_VERSION,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }), encoding="utf-8")


def clear_server_record(port: int) -> None:
    """Remove server.json, but only if it is still ours."""
    path = server_record_path()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if record.get("port") == port and record.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


def fetch_health(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def is_our_server(url: str, health=fetch_health) -> bool:
    """The /health identity check that run.sh used, plus one more test.

    "reference_space" in the payload says it is this app rather than some
    other server on the port. The database path says it is THIS user's
    data: a dev instance pointed at a scratch dir is a different app as
    far as reuse is concerned.
    """
    payload = health(url)
    if not payload or "reference_space" not in payload:
        return False
    return payload.get("database") == str(config.DB_PATH)


def find_running_instance(health=fetch_health) -> str | None:
    """URL of this user's running server, from server.json, or None.

    A stale record (crashed app, port reused by something else) fails the
    identity check and is treated as absent.
    """
    try:
        record = json.loads(server_record_path().read_text(encoding="utf-8"))
        port = int(record["port"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    url = f"http://{HOST}:{port}"
    return url if is_our_server(url, health=health) else None


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

def choose_socket(preferred: int = PREFERRED_PORT) -> socket.socket:
    """A bound loopback socket: the preferred port if free, else any free one.

    Binding here and handing the socket to uvicorn (rather than picking a
    number and letting uvicorn bind it later) leaves no window in which
    another program could take the port.

    127.0.0.1 only, never 0.0.0.0: the dashboard is personal browsing
    history and must not be reachable from the network.
    """
    for port in (preferred, 0):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # Lets a quick relaunch rebind past TIME_WAIT. Not on Windows,
            # where SO_REUSEADDR lets a socket steal a port in active use.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((HOST, port))
            return sock
        except OSError as exc:
            sock.close()
            if port == 0:
                raise
            if exc.errno not in (errno.EADDRINUSE, errno.EACCES, 10048, 10013):
                raise
            log.info("Port %d is taken; using a free port instead", preferred)
    raise AssertionError("unreachable")


class ServerThread:
    """uvicorn in a daemon thread, with a clean stop."""

    def __init__(self, app, sock: socket.socket):
        import uvicorn

        self.sock = sock
        self.port = sock.getsockname()[1]
        self.url = f"http://{HOST}:{self.port}"
        # log_config=None: uvicorn's records go to our root logger (and so
        # the file). No access log: the window polls /jobs every second.
        # ws="none": the app uses no websockets, so do not load a library.
        self.server = uvicorn.Server(uvicorn.Config(
            app, log_config=None, access_log=False, lifespan="off", ws="none",
        ))
        self.thread = threading.Thread(
            target=self.server.run, kwargs={"sockets": [sock]},
            name="uvicorn", daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def wait_ready(self, timeout: float = READY_TIMEOUT_S) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.thread.is_alive():
                return False
            if self.server.started and fetch_health(self.url):
                return True
            time.sleep(0.05)
        return False

    def alive(self) -> bool:
        return self.thread.is_alive()

    def stop(self, timeout: float = 5.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class NativeApi:
    """Native dialogs for the page, as window.pywebview.api.* in JavaScript.

    pywebview exposes this object's public methods to the page and returns
    their results to it as Promises. The setup screens use the pickers so
    nobody has to type a path; in --browser mode there is no pywebview
    object, and setup.js falls back to text fields.

    _window starts with an underscore on purpose: pywebview exposes public
    attributes too, and the window object must not be walked into.
    """

    def __init__(self):
        self._window = None

    def pick_folder(self, start: str = "") -> str | None:
        """A folder chosen in the OS folder dialog, or None if cancelled."""
        import webview

        chosen = self._window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=start or str(Path.home())
        )
        return chosen[0] if chosen else None

    def pick_file(self, start: str = "") -> str | None:
        """A JSON file chosen in the OS open dialog (the client secret)."""
        import webview

        chosen = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=start or str(Path.home() / "Downloads"),
            file_types=("JSON files (*.json)", "All files (*.*)"),
        )
        return chosen[0] if chosen else None


def run_window(url: str, debug: bool = False) -> None:
    """Open the app window and block until it is closed.

    private_mode=False plus a storage_path in the user-data dir is what lets
    localStorage (panel width, stow state) survive a relaunch: pywebview's
    default is a private session that forgets everything on exit.
    text_select=True because pywebview disables text selection by default,
    and people will want to copy a page title or a log path.
    """
    import webview

    native = NativeApi()
    native._window = webview.create_window(
        WINDOW_TITLE, url,
        width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
        min_size=WINDOW_MIN_SIZE, background_color=WINDOW_BACKGROUND,
        text_select=True, js_api=native,
    )
    storage_path = Path(config.USER_DATA_DIR) / "webview"
    storage_path.mkdir(parents=True, exist_ok=True)
    webview.start(private_mode=False, storage_path=str(storage_path), debug=debug)


def run_browser(url: str, server: ServerThread | None) -> None:
    """Open a browser tab; if we own the server, serve until Ctrl-C or kill.

    Both signals are routed to KeyboardInterrupt explicitly. SIGTERM (a
    plain `kill`) would otherwise end the process without the shutdown in
    main(), leaving a job's child running and server.json behind; and a
    process started in the background by a non-interactive shell inherits
    SIGINT as ignored, so Python never installs its Ctrl-C handler.
    """
    webbrowser.open(url)
    if server is None:
        return

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"Wikipedia Wrapped is running at {url} (Ctrl-C to stop)")
    try:
        while server.alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def launch_job_steps(args) -> list[jobs.Step]:
    """The check-on-launch job, honoring the flags.

    --on-launch makes app.sync respect the sync.on_launch setting and the
    minimum interval, so a relaunch shortly after a sync costs nothing.
    """
    return jobs.sync_steps(
        force=args.force_sync,
        on_launch=not args.force_sync,
        sync=not args.no_sync,
        enrich=not args.no_enrich,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Launch the Wikipedia Wrapped app window."
    )
    parser.add_argument("--port", type=int, default=PREFERRED_PORT,
                        help="port to prefer (default %(default)s; any free "
                             "port is used if it is taken)")
    parser.add_argument("--browser", action="store_true",
                        help="open in a normal browser instead of the app "
                             "window (for development and debugging)")
    parser.add_argument("--no-sync", action="store_true",
                        help="skip the check-on-launch history sync")
    parser.add_argument("--force-sync", action="store_true",
                        help="sync even if one ran recently")
    parser.add_argument("--no-enrich", action="store_true",
                        help="skip topic and map enrichment of new pages")
    parser.add_argument("--debug", action="store_true",
                        help="enable the window's web inspector")
    parser.add_argument("--self-test", action="store_true",
                        help="check the installation without opening a "
                             "window, then exit 0 (passed) or 1 (failed)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.self_test:
        # Before logging is set up: the self-test must not create anything
        # in the user-data folder, not even a log file.
        from app import selftest

        return selftest.main()
    log_path = setup_logging()
    log.info(
        "Starting Wikipedia Wrapped %s (Python %s, %s %s)",
        config.APP_VERSION, platform.python_version(), platform.system(),
        platform.machine(),
    )
    log.info("User data: %s", config.USER_DATA_DIR)
    log.info("Resources: %s", config.REFERENCE_DIR)
    log.info("Log file:  %s", log_path)
    legacy = migrate.legacy_status()
    if legacy:
        log.warning(legacy["message"])

    existing = find_running_instance()
    if existing:
        log.info("Already running at %s; opening another window on it", existing)
        if args.browser:
            run_browser(existing, server=None)
        else:
            run_window(existing, debug=args.debug)
        return 0

    from app import api   # deferred: imports the whole app

    runner = jobs.JobRunner()
    sock = choose_socket(args.port)
    server = ServerThread(api.create_app(job_runner=runner), sock)
    server.start()
    if not server.wait_ready():
        log.error("The server did not start; see %s", log_path)
        server.stop()
        return 1
    write_server_record(server.port)
    log.info("Serving at %s", server.url)

    # First run: no settings yet, so the window shows the setup screens and
    # their Finish button starts the first sync, after the user has chosen
    # what to read. Otherwise the sync step itself honors sync.on_launch
    # (--on-launch), so the launcher does not second-guess it; enrichment
    # always runs because it is a no-op when nothing is pending.
    if setup_service.is_first_run():
        log.info("First run: waiting for setup before the first sync")
    else:
        runner.start("launch", launch_job_steps(args))

    try:
        if args.browser:
            run_browser(server.url, server)
        else:
            run_window(server.url, debug=args.debug)
    finally:
        log.info("Shutting down")
        runner.shutdown()
        server.stop()
        clear_server_record(server.port)
    log.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
