"""The read-only resources that ship inside the installers, and how they
are checked.

Two resource sets, each a folder carrying a manifest.json:

    <resources>/models/all-MiniLM-L6-v2/   the sentence-transformers model
    <resources>/reference/                 the frozen UMAP reference space

<resources> is config.resource_dir(): the installed app's bundled
"resources" folder, or the repo's data/ in a dev checkout.

A manifest records a sha256 for every file in its folder, plus what the
files were made with (the model's Hugging Face revision; for the reference
space, the library versions its pickled reducer depends on). That one file
serves three checks, so there is a single definition of "intact":

- the packaging scripts verify downloads before building an installer,
- tests/test_bundle_consistency.py verifies a dev setup,
- the installed app's --self-test verifies what was actually installed.

Why versions matter for the reference space: umap_reducer.pkl is a pickle
of objects from umap-learn, pynndescent, scikit-learn and numba-compiled
code. A pickle can fail to load, or load and quietly misbehave, under other
versions of those libraries. So the versions it was built with travel with
it, and the build refuses a mismatch instead of shipping a broken map.
"""

from __future__ import annotations

import hashlib
import json
import platform
from importlib import metadata
from pathlib import Path

MANIFEST = "manifest.json"
MANIFEST_FORMAT = 1

# Libraries whose exact version a pickled reducer depends on. These must be
# identical between the environment that built the reference space and the
# one that loads it. (Python itself is compared on major.minor only: a patch
# release does not change the pickle format or these libraries' builds.)
PICKLE_PACKAGES = (
    "numpy", "numba", "llvmlite", "umap-learn", "scikit-learn", "pynndescent",
)

# Recorded for the record (they shape the embedding numbers the space was
# built from) but not required to match exactly: a drift there is caught by
# the embedding-consistency check, within a stated tolerance, instead.
EMBEDDING_PACKAGES = ("torch", "sentence-transformers", "transformers", "scipy")


# ---------------------------------------------------------------------------
# Checksums
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_hashes(folder: Path) -> dict[str, str]:
    """sha256 of every file under folder (except the manifest), keyed by
    its POSIX-style relative path so manifests compare across platforms."""
    folder = Path(folder)
    return {
        path.relative_to(folder).as_posix(): sha256_file(path)
        for path in sorted(folder.rglob("*"))
        if path.is_file() and path.name != MANIFEST
    }


def write_manifest(folder: Path, **fields) -> Path:
    """Write folder/manifest.json: the given fields plus every file's sha256.

    Deliberately no "written at" timestamp: the same files must give the
    same manifest, so a packaged archive has a reproducible checksum.
    """
    folder = Path(folder)
    manifest = {
        "format": MANIFEST_FORMAT,
        **fields,
        "files": file_hashes(folder),
    }
    path = folder / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_manifest(folder: Path) -> dict:
    """The folder's manifest; FileNotFoundError if it has none."""
    return json.loads((Path(folder) / MANIFEST).read_text(encoding="utf-8"))


def verify_folder(folder: Path) -> list[str]:
    """Problems with a resource folder against its manifest; [] if intact.

    Missing files and checksum mismatches are problems. Files the manifest
    does not list are ignored (an installer may add none, but a dev
    checkout can hold unrelated files beside the reference space).
    """
    folder = Path(folder)
    try:
        manifest = read_manifest(folder)
    except FileNotFoundError:
        return [f"{folder}: no {MANIFEST}"]
    except json.JSONDecodeError as exc:
        return [f"{folder / MANIFEST}: unreadable ({exc})"]
    problems = []
    for rel, expected in sorted(manifest.get("files", {}).items()):
        path = folder / rel
        if not path.is_file():
            problems.append(f"{path}: missing")
        elif sha256_file(path) != expected:
            problems.append(f"{path}: checksum mismatch")
    return problems


# ---------------------------------------------------------------------------
# Library versions
# ---------------------------------------------------------------------------

def runtime_versions() -> dict[str, str | None]:
    """Installed versions of the libraries a reference space depends on.

    Read from package metadata rather than by importing, so asking is cheap
    and works even where importing torch would be slow.
    """
    versions: dict[str, str | None] = {"python": platform.python_version()}
    for name in (*PICKLE_PACKAGES, *EMBEDDING_PACKAGES):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def version_mismatches(recorded: dict, expected: dict) -> list[str]:
    """Where a reference space's recorded versions differ from `expected`.

    Exact for PICKLE_PACKAGES, major.minor for Python. A version missing on
    either side is a mismatch too: "unknown" must never pass as "same".
    """
    problems = []

    def minor(version):
        return ".".join(str(version).split(".")[:2]) if version else None

    if minor(recorded.get("python")) != minor(expected.get("python")):
        problems.append(
            f"python: built with {recorded.get('python')}, runtime has "
            f"{expected.get('python')} (major.minor must match)"
        )
    for name in PICKLE_PACKAGES:
        if recorded.get(name) is None or recorded.get(name) != expected.get(name):
            problems.append(
                f"{name}: built with {recorded.get(name)}, runtime has {expected.get(name)}"
            )
    return problems


# ---------------------------------------------------------------------------
# What a lock file will install
# ---------------------------------------------------------------------------

# conda package name -> the Python distribution name used above, where the
# two differ.
_CONDA_TO_DIST = {"pytorch": "torch"}


def lock_versions(lock_path: Path) -> dict[str, str]:
    """{name: version} for an explicit conda lock (packaging/locks/*.txt),
    keyed like runtime_versions() so the two can be compared directly.

    Lets packaging check a reference space against the environment an
    installer WILL contain, from the build machine, without installing it.
    """
    import re

    versions = {}
    for line in Path(lock_path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("http"):
            continue
        filename = line.split("#")[0].rsplit("/", 1)[-1]
        stem = re.sub(r"\.(conda|tar\.bz2)$", "", filename)
        name, version, _build = stem.rsplit("-", 2)
        versions[_CONDA_TO_DIST.get(name, name)] = version
    return versions


# ---------------------------------------------------------------------------
# Embedding consistency: is this runtime producing the same vectors?
# ---------------------------------------------------------------------------

# Fixed texts and the vectors the pinned model produced for them in the
# environment that built the reference space (scripts/make_embedding_fixture.py).
# Shipped with the code, so the installed app can check itself.
FIXTURE = Path(__file__).with_name("bundle_fixture.json")

# The worst cosine similarity accepted between a fresh vector and the stored
# one. Different CPUs, BLAS libraries (MKL vs OpenBLAS) and thread counts
# change the last few bits of a float32 result, which is fine; a different
# model revision or a broken library moves vectors far more than 0.001.
COSINE_THRESHOLD = 0.999


def load_fixture(path: Path | None = None) -> dict:
    return json.loads(Path(path or FIXTURE).read_text(encoding="utf-8"))


def fixture_similarities(model, fixture: dict | None = None) -> list[float]:
    """Cosine similarity of each fixture text's fresh embedding to the
    stored one. Every value should be >= COSINE_THRESHOLD."""
    import numpy as np

    fixture = fixture or load_fixture()
    fresh = model.encode(fixture["texts"], convert_to_numpy=True,
                         normalize_embeddings=True, show_progress_bar=False)
    stored = np.asarray(fixture["vectors"], dtype=np.float32)
    stored /= np.linalg.norm(stored, axis=1, keepdims=True)
    return [float(x) for x in np.sum(fresh * stored, axis=1)]
