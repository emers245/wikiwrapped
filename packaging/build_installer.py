"""Build the WikiWrapped installer for this computer's platform.

    conda run -n wikiwrapped-build python packaging/build_installer.py

Makes packaging/build/dist/WikiWrapped-<version>-<platform>.pkg (macOS)
or .exe (Windows). Installers are built on the platform they are for: an
Intel Mac builds the Intel .pkg, the GitHub runners build the rest
(Milestone 4). Everything it writes stays under packaging/build/, which is
git-ignored: downloads, caches and the installer itself.

Before the first build, put the bundled resources in place (once; both
scripts verify what they fetch, and need the dev environment):

    python scripts/fetch_model.py --dest packaging/build/resources
    python scripts/fetch_reference_space.py      # [--archive <file>.tar.gz]

The steps, in order:

1. Check the bundled resources: intact against their manifests, the pinned
   model revision, and a reference space built with the library versions
   this platform's lock installs (app/bundle.py says why that matters).
2. Icons, from packaging/icon/icon.png.
3. The app's own conda package (packaging/recipe, packaging/stage_app.py).
4. The pywebview wheels, downloaded for the target platform and checked
   against the hashes in packaging/wheels/requirements-<platform>.txt.
5. The installer's package list: the platform's lock file plus the app
   package, as one explicit environment file.
6. The extra files copied into the install folder as they are: the
   resources, the wheels, and (macOS) the uninstaller.
7. constructor, with packaging/installer/construct.yaml.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGING = REPO / "packaging"
# The repo for app.bundle (stdlib only: config is never imported here), and
# this folder for stage_app. packaging/ is not a Python package on purpose;
# the name would shadow the `packaging` library that pip and conda use.
sys.path[:0] = [str(REPO), str(PACKAGING)]

import stage_app  # noqa: E402
from app import bundle  # noqa: E402

BUILD = PACKAGING / "build"
RESOURCES = BUILD / "resources"
CHANNEL = BUILD / "channel"
DIST = BUILD / "dist"
# Package downloads, kept here (not in ~/.conda) so a build touches nothing
# outside the repo, and so CI can cache this one folder.
PKGS_CACHE = BUILD / "pkgs"
CONSTRUCTOR_CACHE = BUILD / "constructor-cache"

MODEL_NAME = "all-MiniLM-L6-v2"

# The oldest system each installer supports, as pip platform tags; pip also
# accepts wheels built for older systems and universal2 wheels. macOS 11 is
# what the locked conda packages need (construct.yaml checks it too).
WHEEL_PLATFORMS = {
    "osx-64": "macosx_11_0_x86_64",
    "osx-arm64": "macosx_11_0_arm64",
    "win-64": "win_amd64",
}


def this_platform() -> str:
    if sys.platform == "darwin":
        return "osx-arm64" if platform.machine() == "arm64" else "osx-64"
    if sys.platform == "win32":
        return "win-64"
    raise SystemExit("Installers are built on macOS or Windows only.")


def config_value(name: str) -> str:
    """A constant from config.py, read without importing config (which
    needs the app's dependencies; this runs in the build environment)."""
    tree = ast.parse((REPO / "config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == name):
            return ast.literal_eval(node.value)
    raise SystemExit(f"config.py does not set {name}")


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(cmd: list, **kwargs) -> None:
    print("    $ " + " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def link_or_copy_tree(src: Path, dest: Path) -> None:
    """Hard links where possible: the resources are hundreds of MB, and
    constructor only reads them."""
    def link(s, d):
        try:
            os.link(s, d)
        except OSError:
            shutil.copy2(s, d)
    shutil.copytree(src, dest, copy_function=link)


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

def check_resources(lock: Path) -> dict:
    model = RESOURCES / "models" / MODEL_NAME
    reference = RESOURCES / "reference"
    missing = [p for p in (model, reference) if not (p / bundle.MANIFEST).is_file()]
    if missing:
        raise SystemExit(
            "Bundled resources missing: " + ", ".join(map(str, missing)) + "\n"
            "Fetch them first (in the dev environment):\n"
            "  python scripts/fetch_model.py --dest packaging/build/resources\n"
            "  python scripts/fetch_reference_space.py"
        )
    problems = bundle.verify_folder(model) + bundle.verify_folder(reference)
    pinned = config_value("EMBEDDING_MODEL_REVISION")
    if bundle.read_manifest(model).get("revision") != pinned:
        problems.append(f"model is not the pinned revision {pinned}")
    manifest = bundle.read_manifest(reference)
    problems += bundle.version_mismatches(manifest.get("versions", {}),
                                          bundle.lock_versions(lock))
    if problems:
        raise SystemExit("Bundled resources refused:\n  " + "\n  ".join(problems))
    print(f"    model {pinned[:8]}; reference space {manifest.get('tag')} "
          f"({manifest.get('articles')} articles), versions match {lock.name}")
    return manifest


def build_app_package(version: str) -> Path:
    run([sys.executable, PACKAGING / "icon" / "make_icons.py"])
    shutil.rmtree(CHANNEL, ignore_errors=True)
    run(["rattler-build", "build", "--recipe", PACKAGING / "recipe" / "recipe.yaml",
         "--output-dir", CHANNEL, "-c", "conda-forge"],
        env={**os.environ, "WIKIPEDIA_WRAPPED_VERSION": version})
    packages = sorted((CHANNEL / "noarch").glob(f"wikiwrapped-{version}-*.conda"))
    if len(packages) != 1:
        raise SystemExit(f"Expected one app package in {CHANNEL / 'noarch'}, found {packages}")
    return packages[0]


def download_wheels(target: str, dest: Path) -> Path:
    requirements = PACKAGING / "wheels" / f"requirements-{target}.txt"
    dest.mkdir(parents=True)
    # The hashes in the requirements file put pip in hash-checking mode:
    # a wheel that does not match is refused.
    run([sys.executable, "-m", "pip", "download", "--quiet", "--no-deps",
         "--only-binary", ":all:", "--python-version", "3.11",
         "--platform", WHEEL_PLATFORMS[target], "--dest", dest, "-r", requirements])
    shutil.copy2(requirements, dest / "requirements.txt")
    return dest


def write_environment_file(lock: Path, app_package: Path, dest: Path) -> None:
    """The lock, plus one line for the app package in the same format:
    URL#md5. conda installs exactly these files, nothing solved."""
    md5 = hashlib.md5(app_package.read_bytes()).hexdigest()
    text = lock.read_text(encoding="utf-8").rstrip("\n")
    dest.write_text(f"{text}\n{app_package.resolve().as_uri()}#{md5}\n", encoding="utf-8")


def stage_extra_files(target: str, wheels: Path, extra: Path) -> None:
    resources = extra / stage_app.CODE_DIR / "resources"
    link_or_copy_tree(RESOURCES / "models", resources / "models")
    link_or_copy_tree(RESOURCES / "reference", resources / "reference")
    shutil.move(str(wheels), extra / "wheels")
    if target.startswith("osx-"):
        shutil.copy2(PACKAGING / "installer" / "macos" / "uninstall.sh", extra / "uninstall.sh")


def run_constructor(target: str, version: str, build: Path) -> Path:
    DIST.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WW_VERSION": version,
        "WW_BUILD": build.as_posix(),
        "CONDA_PKGS_DIRS": str(PKGS_CACHE),
        # constructor reads the package list by installing it into a
        # temporary environment; that must not create the app's shortcut
        # on the build machine.
        "CONDA_SHORTCUTS": "false",
    }
    # Unset, constructor uses its own bundled conda (conda-standalone), the
    # same one the installer runs on users' machines, rather than whichever
    # conda happens to be installed here.
    env.pop("CONDA_EXE", None)
    started = time.time()
    run([sys.executable, "-m", "constructor", PACKAGING / "installer",
         "--platform", target, "--output-dir", DIST, "--cache-dir", CONSTRUCTOR_CACHE],
        env=env)
    built = [p for p in DIST.iterdir() if p.stat().st_mtime >= started
             and p.suffix in (".pkg", ".exe")]
    if len(built) != 1:
        raise SystemExit(f"Expected one new installer in {DIST}, found {built}")
    return built[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.parse_args(argv)

    target = this_platform()
    version = stage_app.app_version()
    lock = PACKAGING / "locks" / f"{target}.txt"
    build = BUILD / target
    print(f"Building WikiWrapped {version} for {target}")

    step("Checking the bundled resources")
    check_resources(lock)

    step("Building the app package")
    app_package = build_app_package(version)

    shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True)
    step("Downloading the pywebview wheels")
    wheels = download_wheels(target, build / "wheels-download")

    step("Writing the installer's package list and extra files")
    write_environment_file(lock, app_package, build / "environment.txt")
    stage_extra_files(target, wheels, build / "extra")

    step("Running constructor (several minutes)")
    installer = run_constructor(target, version, build)

    sha = bundle.sha256_file(installer)
    (installer.parent / f"{installer.name}.sha256").write_text(
        f"{sha}  {installer.name}\n", encoding="utf-8")
    print(f"\nBuilt {installer} ({installer.stat().st_size / 1e6:.0f} MB)\nsha256 {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
