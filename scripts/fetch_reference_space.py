"""Download and verify the packaged reference space, for building an installer.

    python scripts/fetch_reference_space.py                        # from the GitHub Release
    python scripts/fetch_reference_space.py --archive path.tar.gz  # a local copy instead
    python scripts/fetch_reference_space.py --dest packaging/build/resources --platform win-64

Puts the reference space in <dest>/reference/ (default:
packaging/build/resources/, the folder an installer is built from) after
three checks, any of which stops the build:

1. The archive's sha256 equals the one committed in
   packaging/reference-space.json. That file, not anything downloaded, is
   the trust anchor: the archive holds a pickle, which runs code when
   loaded, so only the exact bytes the maintainer packaged are accepted.
2. Every extracted file matches the archive's own manifest.json.
3. The library versions the manifest records match the installer's lock
   (packaging/locks/<platform>.txt), and its model revision matches the
   app's pinned one. See app/bundle.py for why.

The repo is private, so downloading needs a token: GH_TOKEN or
GITHUB_TOKEN (CI provides the latter), or a logged-in `gh` CLI.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import requests

# Standalone script: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import bundle  # noqa: E402

ANCHOR = config.PROJECT_ROOT / "packaging" / "reference-space.json"
LOCKS = config.PROJECT_ROOT / "packaging" / "locks"
DEFAULT_DEST = config.PROJECT_ROOT / "packaging" / "build" / "resources"
API = "https://api.github.com"


def this_platform() -> str:
    """The conda platform name of this machine (osx-64, osx-arm64, win-64)."""
    if sys.platform == "darwin":
        return "osx-arm64" if platform.machine() == "arm64" else "osx-64"
    if sys.platform == "win32":
        return "win-64"
    return "linux-64"


def github_token() -> str | None:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def download(anchor: dict, out: Path) -> None:
    """The release asset, through the GitHub API (works for private repos)."""
    token = github_token()
    if not token:
        raise SystemExit(
            "No GitHub token: set GH_TOKEN (or log in with `gh auth login`); "
            "the repository is private."
        )
    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json"}
    release = requests.get(f"{API}/repos/{anchor['repo']}/releases/tags/{anchor['tag']}",
                           headers=headers, timeout=30)
    if release.status_code == 404:
        raise SystemExit(f"No release {anchor['tag']} in {anchor['repo']} (uploaded yet?).")
    release.raise_for_status()
    assets = {a["name"]: a for a in release.json().get("assets", [])}
    if anchor["asset"] not in assets:
        raise SystemExit(f"Release {anchor['tag']} has no asset {anchor['asset']}.")
    # Asking for octet-stream returns the file itself (via a redirect, on
    # which requests drops the Authorization header, as it should).
    with requests.get(assets[anchor["asset"]]["url"], stream=True, timeout=60,
                      headers={**headers, "Accept": "application/octet-stream"}) as r:
        r.raise_for_status()
        with open(out, "wb") as handle:
            for chunk in r.iter_content(1 << 20):
                handle.write(chunk)


def install(archive: Path, dest: Path, lock: Path, anchor: dict) -> dict:
    """Verify archive -> extract to dest/reference -> verify contents."""
    sha = bundle.sha256_file(archive)
    if sha != anchor["sha256"]:
        raise SystemExit(
            f"{archive.name}: sha256 {sha} does not match the committed "
            f"{anchor['sha256']} (packaging/reference-space.json). Refusing it."
        )
    target = dest / "reference"
    with tempfile.TemporaryDirectory(prefix="wikiwrapped-reference-") as tmp:
        with tarfile.open(archive, "r:gz") as tar:
            # The "data" filter refuses absolute paths, "..", links and
            # device files: an archive can only write plain files below tmp.
            tar.extractall(tmp, filter="data")
        staged = Path(tmp) / "reference"
        problems = bundle.verify_folder(staged)
        manifest = bundle.read_manifest(staged)
        expected = bundle.lock_versions(lock)
        problems += [f"{lock.stem}: {p}" for p in
                     bundle.version_mismatches(manifest.get("versions", {}), expected)]
        if manifest.get("embedding_model_revision") != config.EMBEDDING_MODEL_REVISION:
            problems.append(
                f"model revision {manifest.get('embedding_model_revision')} != "
                f"pinned {config.EMBEDDING_MODEL_REVISION}"
            )
        if problems:
            raise SystemExit("Reference space refused:\n  " + "\n  ".join(problems))
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged, target)
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--archive", type=Path, default=None,
                        help="use this local archive instead of downloading")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST,
                        help="resources root to fill (default %(default)s)")
    parser.add_argument("--platform", default=this_platform(),
                        help="which lock to check versions against "
                             "(default: this machine, %(default)s)")
    args = parser.parse_args(argv)

    anchor = json.loads(ANCHOR.read_text(encoding="utf-8"))
    lock = LOCKS / f"{args.platform}.txt"
    if not lock.exists():
        raise SystemExit(f"No lock for platform {args.platform}: {lock}")

    with tempfile.TemporaryDirectory(prefix="wikiwrapped-download-") as tmp:
        archive = args.archive
        if archive is None:
            archive = Path(tmp) / anchor["asset"]
            print(f"Downloading {anchor['asset']} from {anchor['repo']} release {anchor['tag']}")
            download(anchor, archive)
        manifest = install(archive, args.dest, lock, anchor)

    print(f"Verified and installed {anchor['tag']} into {args.dest / 'reference'}: "
          f"{manifest.get('articles')} articles, built {manifest.get('built_at')}, "
          f"versions match {lock.name}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
