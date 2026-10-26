"""The bundled resources (model, reference space) and the checks on them.

The tests that need the model skip cleanly when it has not been fetched
(python scripts/fetch_model.py); CI fetches it, so they always run there.
They are what catch an installer whose model or libraries quietly differ
from what built the map: the embedding check compares fresh vectors with
ones stored when the reference space was built (COSINE_THRESHOLD).
"""

import json
import socket

import numpy as np
import pytest

import config
from app import bundle, embeddings, umap_space

MODEL_PRESENT = (config.EMBEDDING_MODEL_DIR / "modules.json").is_file()
needs_model = pytest.mark.skipif(
    not MODEL_PRESENT, reason="bundled model not fetched (scripts/fetch_model.py)"
)


@pytest.fixture
def fresh_model(monkeypatch):
    """A model loaded for this test, not the cached singleton."""
    monkeypatch.setattr(embeddings, "_model", None)
    return embeddings


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to open a connection fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError(f"network access attempted: {args[:1]}")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

@needs_model
def test_bundled_model_matches_its_manifest_and_the_pins():
    assert bundle.verify_folder(config.EMBEDDING_MODEL_DIR) == []
    manifest = bundle.read_manifest(config.EMBEDDING_MODEL_DIR)
    assert manifest["revision"] == config.EMBEDDING_MODEL_REVISION
    pins = json.loads((config.PROJECT_ROOT / "packaging" / "model.json").read_text())
    assert manifest["files"] == pins["files"]


@needs_model
def test_bundled_model_loads_without_any_network(fresh_model, no_network):
    model = fresh_model.get_model()
    vector = model.encode(["Douglas Adams"], normalize_embeddings=True)
    assert vector.shape == (1, 384)


@needs_model
def test_embeddings_match_the_fixture(fresh_model):
    fixture = bundle.load_fixture()
    assert fixture["revision"] == config.EMBEDDING_MODEL_REVISION
    similarities = bundle.fixture_similarities(fresh_model.get_model(), fixture)
    assert min(similarities) > bundle.COSINE_THRESHOLD, similarities


@needs_model
def test_reducer_loads_and_projects_the_fixture():
    vectors = np.asarray(bundle.load_fixture()["vectors"], dtype=np.float32)
    coords = umap_space.load_reducer().transform(vectors)
    assert coords.shape == (len(vectors), 2) and np.isfinite(coords).all()


def test_installed_mode_refuses_to_download_a_missing_model(tmp_path, monkeypatch, fresh_model):
    """A broken install must say so, not quietly fetch a replacement."""
    for name in embeddings.OFFLINE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "is_installed", lambda: True)
    monkeypatch.setattr(config, "EMBEDDING_MODEL_DIR", tmp_path / "missing")
    with pytest.raises(embeddings.ModelMissing, match="Reinstall"):
        fresh_model.get_model()
    import os
    assert all(os.environ.get(k) == v for k, v in embeddings.OFFLINE_ENV.items())


def test_installed_mode_is_off_in_a_checkout():
    assert not (config.PROJECT_ROOT / config.INSTALLED_RESOURCES_NAME).exists()
    assert config.is_installed() is False


def test_installed_resources_are_found_next_to_the_code(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    assert config.installed_resource_dir() is None
    (tmp_path / "resources").mkdir()
    assert config.installed_resource_dir() == tmp_path / "resources"
    monkeypatch.delenv(config.RESOURCE_ENV_VAR, raising=False)
    assert config.resource_dir() == tmp_path / "resources"


# ---------------------------------------------------------------------------
# Manifests and versions
# ---------------------------------------------------------------------------

def test_manifest_round_trip_and_tamper_detection(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"\x00\x01")
    bundle.write_manifest(tmp_path, kind="test")
    assert bundle.verify_folder(tmp_path) == []
    assert set(bundle.read_manifest(tmp_path)["files"]) == {"a.txt", "sub/b.bin"}

    (tmp_path / "a.txt").write_text("changed")
    (tmp_path / "sub" / "b.bin").unlink()
    problems = bundle.verify_folder(tmp_path)
    assert any("a.txt: checksum mismatch" in p for p in problems)
    assert any("b.bin: missing" in p for p in problems)


def test_manifests_are_reproducible(tmp_path):
    """No timestamps: the same files always give the same manifest, so a
    packaged archive's checksum is reproducible."""
    (tmp_path / "a.txt").write_text("hello")
    first = bundle.write_manifest(tmp_path, kind="test").read_bytes()
    assert bundle.write_manifest(tmp_path, kind="test").read_bytes() == first


def test_version_mismatches():
    base = {"python": "3.11.15", "numpy": "1.26.4", "numba": "0.60.0",
            "llvmlite": "0.43.0", "umap-learn": "0.5.7",
            "scikit-learn": "1.5.2", "pynndescent": "0.5.13"}
    assert bundle.version_mismatches(base, {**base, "python": "3.11.17"}) == []
    assert bundle.version_mismatches(base, {**base, "python": "3.12.1"})
    assert bundle.version_mismatches(base, {**base, "pynndescent": "0.6.0"})
    unknown = {k: v for k, v in base.items() if k != "numba"}
    assert bundle.version_mismatches(unknown, base), "unknown must not pass as same"


@pytest.mark.parametrize("platform", ["osx-64", "osx-arm64", "win-64"])
def test_lock_versions_read_the_pinned_cluster(platform):
    versions = bundle.lock_versions(config.PROJECT_ROOT / "packaging" / "locks" / f"{platform}.txt")
    assert versions["numpy"] == "1.26.4" and versions["umap-learn"] == "0.5.7"
    assert versions["torch"] == "2.10.0" and versions["python"].startswith("3.11.")


def test_the_committed_reference_anchor_matches_every_lock():
    """packaging/reference-space.json must describe a space every
    installer can load."""
    anchor = json.loads((config.PROJECT_ROOT / "packaging" / "reference-space.json").read_text())
    assert anchor["embedding_model_revision"] == config.EMBEDDING_MODEL_REVISION
    assert len(anchor["sha256"]) == 64
    for lock in sorted((config.PROJECT_ROOT / "packaging" / "locks").glob("*.txt")):
        assert bundle.version_mismatches(anchor["versions"], bundle.lock_versions(lock)) == [], lock.stem


def test_packaging_refuses_a_space_built_with_other_versions(tmp_path, monkeypatch):
    import importlib.util
    path = config.PROJECT_ROOT / "scripts" / "package_reference_space.py"
    spec = importlib.util.spec_from_file_location("package_reference_space", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ANCHOR", tmp_path / "anchor.json")
    monkeypatch.setattr(module, "BUILD_DIR", tmp_path / "build")

    src = tmp_path / "space"
    src.mkdir()
    for name in module.RUNTIME_FILES:
        (src / name).write_text("{}")
    versions = {**bundle.runtime_versions(), "pynndescent": "0.6.0"}
    (src / umap_space.META_FILE).write_text(json.dumps({
        "titles_fitted": 3, "versions": versions,
        "embedding_model_revision": config.EMBEDDING_MODEL_REVISION,
    }))
    with pytest.raises(SystemExit, match="pynndescent"):
        module.main(["--tag", "reference-test", "--src", str(src)])
    assert not (tmp_path / "anchor.json").exists()
