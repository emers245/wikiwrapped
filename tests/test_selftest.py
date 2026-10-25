"""app.selftest: the headless installation check (launcher --self-test).

The real checks import torch, load the model and spawn a child Python, so
the end-to-end run is exercised against an actual installation (the
installer pipeline runs it); here each piece is tested on its own, and the
runner with stand-in checks. Nothing here imports pywebview, which
test_launcher asserts stays unimported."""

import socket

import pytest

import config
from app import bundle, launcher, selftest


def test_snapshot_and_changes_report_every_kind_of_write(tmp_path):
    (tmp_path / "kept.txt").write_text("same")
    (tmp_path / "edited.txt").write_text("before")
    (tmp_path / "deleted.txt").write_text("gone soon")
    before = selftest.snapshot([tmp_path])

    (tmp_path / "edited.txt").write_text("after, and longer")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "new.pyc").write_bytes(b"\x00")
    diff = selftest.changes(before, selftest.snapshot([tmp_path]))

    assert [line.split()[0] for line in diff] == ["added", "modified", "removed"]
    assert diff[0].endswith("new.pyc") and diff[1].endswith("edited.txt")
    assert selftest.changes(before, before) == []


def test_the_code_folder_is_watched_once_when_inside_the_installation(monkeypatch, tmp_path):
    prefix = tmp_path / "prefix"
    code = prefix / "share" / "wikipedia-wrapped"
    code.mkdir(parents=True)
    monkeypatch.setattr(selftest.sys, "prefix", str(prefix))
    monkeypatch.setattr(config, "PROJECT_ROOT", code)
    assert selftest.watched_roots() == [prefix.resolve()]

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", checkout)
    assert selftest.watched_roots() == [prefix.resolve(), checkout.resolve()]


def test_network_is_blocked_only_inside_the_block():
    connect = socket.socket.connect
    with selftest.NetworkBlocked():
        with pytest.raises(OSError, match="offline"):
            socket.create_connection(("127.0.0.1", 9))
    assert socket.socket.connect is connect


def write_resources(tmp_path, monkeypatch, versions):
    model = tmp_path / "models" / "all-MiniLM-L6-v2"
    reference = tmp_path / "reference"
    model.mkdir(parents=True, exist_ok=True)
    reference.mkdir(exist_ok=True)
    (model / "modules.json").write_text("[]")
    (reference / "umap_reducer.pkl").write_bytes(b"pickle")
    bundle.write_manifest(model, kind="model", revision=config.EMBEDDING_MODEL_REVISION)
    bundle.write_manifest(reference, kind="reference", tag="reference-test",
                          articles=3, versions=versions)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_DIR", model)
    monkeypatch.setattr(config, "REFERENCE_DIR", reference)
    return model, reference


def test_resources_pass_when_intact_and_built_with_this_runtime(tmp_path, monkeypatch):
    write_resources(tmp_path, monkeypatch, bundle.runtime_versions())
    assert "reference-test (3 articles)" in selftest.check_resources()


def test_resources_fail_on_tampering_or_other_library_versions(tmp_path, monkeypatch):
    versions = {**bundle.runtime_versions(), "numba": "0.0.1"}
    _model, reference = write_resources(tmp_path, monkeypatch, versions)
    with pytest.raises(AssertionError, match="numba"):
        selftest.check_resources()

    write_resources(tmp_path, monkeypatch, bundle.runtime_versions())
    (reference / "umap_reducer.pkl").write_bytes(b"something else")
    with pytest.raises(AssertionError, match="checksum mismatch"):
        selftest.check_resources()


@pytest.fixture
def stand_in_checks(monkeypatch, tmp_path):
    """Every check passes instantly; the install folder is tmp_path."""
    monkeypatch.setattr(selftest, "watched_roots", lambda: [tmp_path])
    monkeypatch.setattr(selftest, "check_imports", lambda: "ok")
    monkeypatch.setattr(selftest, "check_resources", lambda: "ok")
    monkeypatch.setattr(selftest, "check_model", lambda state: "ok")
    monkeypatch.setattr(selftest, "check_reducer", lambda state: "ok")
    monkeypatch.setattr(selftest, "check_job_interpreter", lambda env: "ok")
    monkeypatch.setenv("NUMBA_CACHE_DIR", "kept")   # restored afterwards
    return tmp_path


def test_all_checks_passing_exits_zero(stand_in_checks, capsys):
    assert selftest.main() == 0
    assert "Self-test passed: all 6 checks." in capsys.readouterr().out


def test_a_failing_check_or_a_write_into_the_install_folder_fails(
        stand_in_checks, monkeypatch, capsys):
    def writes_a_file(env):
        (stand_in_checks / "__pycache__").mkdir()
        (stand_in_checks / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        return "ok"

    def breaks():
        raise ImportError("No module named 'torch'")

    monkeypatch.setattr(selftest, "check_job_interpreter", writes_a_file)
    monkeypatch.setattr(selftest, "check_imports", breaks)
    assert selftest.main() == 1
    out = capsys.readouterr().out
    assert "FAIL  imports" in out and "No module named 'torch'" in out
    assert "FAIL  install folder untouched" in out and "x.pyc" in out
    assert "Self-test FAILED: 2 of 6 checks." in out


def test_the_launcher_flag_runs_the_self_test_before_any_logging(tmp_path, monkeypatch):
    """No log file: the self-test must leave the user-data folder alone."""
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(selftest, "main", lambda: 7)
    assert launcher.main(["--self-test"]) == 7
    assert not (tmp_path / "logs").exists()
