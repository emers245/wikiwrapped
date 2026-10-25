"""Check that an installation works, without opening a window.

    <installed python> <app folder>/launch.py --self-test
    python -m app.launcher --self-test           (a dev checkout)

Prints one line per check and exits 0 if all of them pass, 1 otherwise.
The release pipeline installs every installer silently and runs this; a
user or maintainer can run it when something seems broken.

The checks, and what each one catches:

1. imports: every module of the app and every library it needs, the
   window's included. A package missing from the installer, or (on
   Windows) a DLL that cannot load, shows up here instead of at the first
   sync. The installers also run this check once at the end of installing
   (installer/*/post_install), to get each library's first load over with.
2. resources: the bundled model and reference space match their manifests
   (app/bundle.py), the model is the pinned revision, and the reference
   space was built with this runtime's library versions. The reducer is a
   pickle, which can fail or quietly misbehave under other versions.
3. model: loads from the bundled copy with networking blocked, and embeds
   the fixture texts to the vectors stored when the map was built.
4. reducer: loads and places those fresh vectors on the 2-D map.
5. job interpreter: the Python the background jobs run under
   (jobs.child_python, python.exe rather than pythonw.exe on Windows)
   starts and imports the app.
6. install folder untouched: none of the above wrote a file into the
   installation or the code folder. An installed app must only write to
   the user-data folder: the install folder may be read-only, and a later
   upgrade replaces it wholesale.

The user's data are never touched: nothing here opens the database, and
numba's compile cache goes to a temporary folder for the duration.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import config

# Imported by name, beyond what importing the app's own modules pulls in,
# because several are only imported lazily (at first use) by the app.
LIBRARIES = (
    "numpy", "numba", "llvmlite", "umap", "pynndescent", "sklearn", "scipy",
    "torch", "transformers", "sentence_transformers", "fastapi", "uvicorn",
    "requests", "networkx", "googleapiclient", "google_auth_oauthlib",
    "platformdirs", "tzlocal", "tqdm", "webview",
)

# The window's own libraries, which pywebview imports only when the window
# opens: pyobjc's frameworks on macOS, pythonnet (.NET) on Windows. Imported
# directly, not through pywebview's backend, which would start the GUI.
WINDOW_LIBRARIES = {
    "darwin": ("objc", "AppKit", "Foundation", "WebKit"),
    "win32": ("clr",),
}

# The app's own packages, every module of which must import.
APP_PACKAGES = ("app", "extract")

# What the job-interpreter check imports in the child: the entry points the
# jobs run, plus the heavy native libraries they load.
CHILD_IMPORTS = "import app.sync, app.embeddings, app.umap_space, numpy, torch, umap"

CHILD_TIMEOUT_S = 300


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    seconds: float


# ---------------------------------------------------------------------------
# Watching the install folder
# ---------------------------------------------------------------------------

def watched_roots() -> list[Path]:
    """The installation (the Python environment) and the app's code folder.

    In an installed app the code folder lies inside the environment, so it
    is covered once; in a dev checkout they are separate.
    """
    roots = [Path(sys.prefix).resolve()]
    code = config.PROJECT_ROOT.resolve()
    if not code.is_relative_to(roots[0]):
        roots.append(code)
    return roots


def snapshot(roots: list[Path]) -> dict[str, tuple[int, int]]:
    """{path: (size, mtime in ns)} for every file below the roots."""
    files = {}
    for root in roots:
        for folder, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(folder, name)
                try:
                    info = os.lstat(path)
                except OSError:
                    continue
                files[path] = (info.st_size, info.st_mtime_ns)
    return files


def changes(before: dict, after: dict) -> list[str]:
    """Human-readable differences between two snapshots, sorted."""
    added = [f"added {p}" for p in after.keys() - before.keys()]
    removed = [f"removed {p}" for p in before.keys() - after.keys()]
    modified = [f"modified {p}" for p in before.keys() & after.keys()
                if before[p] != after[p]]
    return sorted(added + removed + modified)


class NetworkBlocked:
    """Within the block, any attempt to open a connection raises."""

    def __enter__(self):
        def refuse(*args, **kwargs):
            raise OSError("network access attempted during an offline check")

        self._saved = (socket.socket.connect, socket.create_connection)
        socket.socket.connect = refuse
        socket.create_connection = refuse
        return self

    def __exit__(self, *exc):
        socket.socket.connect, socket.create_connection = self._saved
        return False


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------

def check_imports() -> str:
    modules = []
    for package_name in APP_PACKAGES:
        package = importlib.import_module(package_name)
        modules.append(package_name)
        for info in pkgutil.walk_packages(package.__path__, f"{package_name}."):
            importlib.import_module(info.name)
            modules.append(info.name)
    libraries = (*LIBRARIES, *WINDOW_LIBRARIES.get(sys.platform, ()))
    for name in libraries:
        importlib.import_module(name)
    return f"{len(modules)} app modules, {len(libraries)} libraries"


def check_resources() -> str:
    from app import bundle

    problems = []
    for folder in (config.EMBEDDING_MODEL_DIR, config.REFERENCE_DIR):
        problems += bundle.verify_folder(folder)
    if problems:
        raise AssertionError("; ".join(problems))
    model = bundle.read_manifest(config.EMBEDDING_MODEL_DIR)
    if model.get("revision") != config.EMBEDDING_MODEL_REVISION:
        raise AssertionError(
            f"model revision {model.get('revision')} is not the pinned "
            f"{config.EMBEDDING_MODEL_REVISION}"
        )
    reference = bundle.read_manifest(config.REFERENCE_DIR)
    mismatches = bundle.version_mismatches(
        reference.get("versions", {}), bundle.runtime_versions()
    )
    if mismatches:
        raise AssertionError("reference space: " + "; ".join(mismatches))
    return (f"model {config.EMBEDDING_MODEL_REVISION[:8]}, reference space "
            f"{reference.get('tag')} ({reference.get('articles')} articles)")


def check_model(state: dict) -> str:
    from app import bundle, embeddings

    if not (config.EMBEDDING_MODEL_DIR / "modules.json").is_file():
        raise AssertionError(f"no bundled model in {config.EMBEDDING_MODEL_DIR}")
    fixture = bundle.load_fixture()
    with NetworkBlocked():
        model = embeddings.get_model()
        similarities = bundle.fixture_similarities(model, fixture)
        state["vectors"] = model.encode(
            fixture["texts"], convert_to_numpy=True,
            normalize_embeddings=True, show_progress_bar=False,
        )
    worst = min(similarities)
    if worst < bundle.COSINE_THRESHOLD:
        raise AssertionError(
            f"embeddings drifted: worst cosine {worst:.6f} < {bundle.COSINE_THRESHOLD}"
        )
    return f"offline, {len(similarities)} fixture texts, worst cosine {worst:.7f}"


def check_reducer(state: dict) -> str:
    import numpy as np

    from app import umap_space

    vectors = state.get("vectors")
    if vectors is None:
        raise AssertionError("skipped: the model check did not produce vectors")
    coords = umap_space.load_reducer().transform(vectors)
    if coords.shape != (len(vectors), 2) or not np.isfinite(coords).all():
        raise AssertionError(f"bad coordinates: shape {coords.shape}")
    return f"placed {len(vectors)} texts on the map"


def check_job_interpreter(env: dict) -> str:
    from app import jobs

    python = jobs.child_python()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    result = subprocess.run(
        [python, "-c", CHILD_IMPORTS], cwd=str(config.PROJECT_ROOT), env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=CHILD_TIMEOUT_S, creationflags=flags,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-1:]
        raise AssertionError(f"{python} exited {result.returncode}: {' '.join(tail)}")
    return Path(python).name


# ---------------------------------------------------------------------------
# Running them
# ---------------------------------------------------------------------------

def run_check(name: str, check, *args) -> Result:
    start = time.monotonic()
    try:
        detail = check(*args)
        ok = True
    except Exception as exc:     # a failed check is reported, never raised
        ok = False
        detail = f"{type(exc).__name__}: {exc}"
    return Result(name, ok, detail, time.monotonic() - start)


def run() -> list[Result]:
    # Never write bytecode while the install folder is being watched (the
    # installed launcher already sets this; a dev run may not have).
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix="wikiwrapped-selftest-") as tmp:
        # numba reads NUMBA_CACHE_DIR when it is first imported, which is
        # below; the user's cache folder is left alone.
        os.environ["NUMBA_CACHE_DIR"] = tmp
        child_env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

        roots = watched_roots()
        before = snapshot(roots)
        state: dict = {}
        results = [
            run_check("imports", check_imports),
            run_check("resources", check_resources),
            run_check("model", check_model, state),
            run_check("reducer", check_reducer, state),
            run_check("job interpreter", check_job_interpreter, child_env),
        ]
        start = time.monotonic()
        diff = changes(before, snapshot(roots))
        shown = "; ".join(diff[:5]) + (f" (and {len(diff) - 5} more)" if len(diff) > 5 else "")
        results.append(Result(
            "install folder untouched", not diff,
            shown or f"{len(before)} files unchanged under {', '.join(map(str, roots))}",
            time.monotonic() - start,
        ))
    return results


def main() -> int:
    print(f"Wikipedia Wrapped {config.APP_VERSION} self-test "
          f"({'installed' if config.is_installed() else 'dev checkout'}, "
          f"Python {sys.version.split()[0]}, {sys.platform})", flush=True)
    results = run()
    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'}  {result.name:<25} "
              f"{result.seconds:6.1f}s  {result.detail}", flush=True)
    failed = [r for r in results if not r.ok]
    if failed:
        print(f"Self-test FAILED: {len(failed)} of {len(results)} checks.")
        return 1
    print(f"Self-test passed: all {len(results)} checks.")
    return 0
