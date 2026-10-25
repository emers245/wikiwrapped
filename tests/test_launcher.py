"""app.launcher: port choice, the running-instance check, and a real server.

pywebview is never imported here: the window is the one part that needs a
display, and it is covered by the manual checklist."""

import json
import logging
import socket
import sys
import urllib.request

import pytest

import config
from app import launcher


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "app.db")
    return tmp_path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_importing_the_launcher_does_not_import_pywebview():
    assert "webview" not in sys.modules


# ---------------------------------------------------------------------------
# Port choice
# ---------------------------------------------------------------------------

def test_the_preferred_port_is_used_when_free():
    port = free_port()
    sock = launcher.choose_socket(port)
    try:
        assert sock.getsockname() == ("127.0.0.1", port)
    finally:
        sock.close()


def test_a_taken_port_falls_back_to_any_free_one():
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    taken = blocker.getsockname()[1]
    try:
        sock = launcher.choose_socket(taken)
        try:
            host, port = sock.getsockname()
            assert host == "127.0.0.1" and port != taken
        finally:
            sock.close()
    finally:
        blocker.close()


# ---------------------------------------------------------------------------
# The running-instance check
# ---------------------------------------------------------------------------

def test_identity_check_requires_this_app_and_this_users_database(userdata):
    ours = {"reference_space": None, "database": str(config.DB_PATH)}
    assert launcher.is_our_server("u", health=lambda url: ours)
    assert not launcher.is_our_server("u", health=lambda url: None)
    assert not launcher.is_our_server("u", health=lambda url: {"status": "ok"})
    other = {"reference_space": None, "database": "/somewhere/else/app.db"}
    assert not launcher.is_our_server("u", health=lambda url: other)


def test_no_record_means_no_running_instance(userdata):
    assert launcher.find_running_instance(health=lambda url: None) is None


def test_a_stale_record_is_ignored(userdata):
    launcher.write_server_record(free_port())
    assert launcher.find_running_instance(health=lambda url: None) is None


def test_a_live_record_is_reused(userdata):
    port = free_port()
    launcher.write_server_record(port)
    healthy = {"reference_space": None, "database": str(config.DB_PATH)}
    url = launcher.find_running_instance(health=lambda url: healthy)
    assert url == f"http://127.0.0.1:{port}"


def test_clearing_the_record_only_removes_our_own(userdata):
    launcher.write_server_record(1234)
    launcher.clear_server_record(9999)                  # not ours: kept
    assert launcher.server_record_path().exists()
    launcher.clear_server_record(1234)
    assert not launcher.server_record_path().exists()


# ---------------------------------------------------------------------------
# Flags -> the launch job
# ---------------------------------------------------------------------------

def test_launch_job_honors_the_flags():
    default = launcher.launch_job_steps(launcher.parse_args([]))
    assert default[0].argv[-1] == "--on-launch" and len(default) == 2

    forced = launcher.launch_job_steps(launcher.parse_args(["--force-sync"]))
    assert forced[0].argv[-1] == "--force"

    no_sync = launcher.launch_job_steps(launcher.parse_args(["--no-sync"]))
    assert [s.label for s in no_sync] == ["Adding topics and map positions"]

    nothing = launcher.parse_args(["--no-sync", "--no-enrich"])
    assert launcher.launch_job_steps(nothing) == []


# ---------------------------------------------------------------------------
# A real server in a thread, the way the launcher runs it
# ---------------------------------------------------------------------------

def test_server_thread_serves_on_loopback_and_stops_cleanly(tmp_path):
    from app import api, jobs

    app = api.create_app(db_path=tmp_path / "app.db",
                         reference_dir=tmp_path / "none",
                         job_runner=jobs.JobRunner(cwd=tmp_path))
    server = launcher.ServerThread(app, launcher.choose_socket(free_port()))
    server.start()
    try:
        assert server.wait_ready(timeout=15)
        assert server.url.startswith("http://127.0.0.1:")
        with urllib.request.urlopen(f"{server.url}/jobs", timeout=5) as response:
            assert json.load(response)["status"] == "idle"
    finally:
        server.stop()
    assert not server.alive()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def test_logging_goes_to_a_rotating_file_in_the_log_dir(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    hook = sys.excepthook
    try:
        path = launcher.setup_logging(tmp_path / "logs")
        logging.getLogger("launcher").info("hello from the test")
        for handler in root.handlers:
            handler.flush()
        assert path == tmp_path / "logs" / "app.log"
        assert "hello from the test" in path.read_text(encoding="utf-8")
    finally:
        for handler in root.handlers[:]:
            if handler not in before:
                root.removeHandler(handler)
                handler.close()
        sys.excepthook = hook
        logging.captureWarnings(False)
