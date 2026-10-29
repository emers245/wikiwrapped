"""Package a built reference space as a versioned, checksummed release asset.

    python scripts/package_reference_space.py --tag reference-v1
    python scripts/package_reference_space.py --tag reference-v0 --versions-from-env

Run after scripts/build_reference_space.py. Produces
packaging/build/reference-space-<version>.tar.gz, updates the committed
trust anchor packaging/reference-space.json, and prints the `gh` commands
that upload the archive to a GitHub Release. The upload is left to the
maintainer on purpose: it publishes a file.

What goes in, and why:

- Only the three files the app reads at runtime: umap_reducer.pkl,
  reference_points.json, reference_meta.json. reference_embeddings.npy is
  written by the build but never read by the app (the reducer pickle
  already holds what .transform() needs), so it stays out: about 61 MB
  saved at full scale.
- A manifest.json (app/bundle.py): each file's sha256, the article count,
  the model revision the embeddings came from, and the exact versions of
  the libraries the pickled reducer depends on.

What is refused:

- A space whose recorded library versions differ from what the installers
  will contain (packaging/locks/*.txt). A pickle loaded under other
  versions can fail or quietly misbehave, so this stops the build here
  rather than on a user's machine.
- A space with no recorded versions, unless --versions-from-env says the
  CURRENT environment is the one that built it (true for the July test
  build, made before builds recorded versions; the environment's pins have
  not moved since). The recorded values are then this environment's.

The archive is deterministic (sorted entries, fixed timestamps and owners),
so packaging the same files twice gives the same sha256.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

# Standalone script: make the repo root importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from app import bundle, umap_space  # noqa: E402

REPO = "emers245/wikiwrapped"
ANCHOR = config.PROJECT_ROOT / "packaging" / "reference-space.json"
BUILD_DIR = config.PROJECT_ROOT / "packaging" / "build"
LOCKS = config.PROJECT_ROOT / "packaging" / "locks"

RUNTIME_FILES = (umap_space.REDUCER_FILE, umap_space.POINTS_FILE, umap_space.META_FILE)


def check_against_locks(versions: dict) -> None:
    """Refuse unless every installer's runtime matches the recorded versions."""
    problems = []
    for lock in sorted(LOCKS.glob("*.txt")):
        expected = bundle.lock_versions(lock)
        for problem in bundle.version_mismatches(versions, expected):
            problems.append(f"{lock.stem}: {problem}")
    if problems:
        raise SystemExit(
            "This reference space was built with library versions the "
            "installers will not contain. Rebuild it in the current "
            "environment (packaging/README.md):\n  " + "\n  ".join(problems)
        )


def deterministic_tar(src: Path, out: Path) -> None:
    """tar.gz of src/ (as reference/...) that hashes the same every time."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            info = tarfile.TarInfo(f"reference/{path.relative_to(src).as_posix()}")
            info.size = path.stat().st_size
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with open(path, "rb") as handle:
                tar.addfile(info, handle)
    with open(out, "wb") as handle:
        with gzip.GzipFile(filename="", mode="wb", fileobj=handle, mtime=0) as gz:
            gz.write(raw.getvalue())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", required=True,
                        help="release tag, e.g. reference-v1 (the asset is "
                             "named reference-space-v1.tar.gz)")
    parser.add_argument("--src", type=Path, default=config.DEV_REFERENCE_DIR,
                        help="built reference space (default %(default)s)")
    parser.add_argument("--versions-from-env", action="store_true",
                        help="record THIS environment's versions for a space "
                             "built before versions were recorded")
    args = parser.parse_args(argv)

    if not args.tag.startswith("reference-"):
        raise SystemExit("--tag must look like reference-v1")
    version = args.tag.removeprefix("reference-")
    meta = json.loads((args.src / umap_space.META_FILE).read_text(encoding="utf-8"))

    versions = meta.get("versions")
    revision = meta.get("embedding_model_revision")
    if versions is None or revision is None:
        if not args.versions_from_env:
            raise SystemExit(
                f"{args.src / umap_space.META_FILE} records no library versions "
                "or model revision (it predates them). If THIS environment "
                "built it, re-run with --versions-from-env; otherwise rebuild."
            )
        versions = bundle.runtime_versions()
        revision = config.EMBEDDING_MODEL_REVISION
        print("Recording this environment's versions (--versions-from-env).")
    if revision != config.EMBEDDING_MODEL_REVISION:
        raise SystemExit(
            f"Built with model revision {revision}, but the app pins "
            f"{config.EMBEDDING_MODEL_REVISION}. Rebuild the reference space."
        )
    check_against_locks(versions)

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    asset = BUILD_DIR / f"reference-space-{version}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="wikiwrapped-reference-") as tmp:
        stage = Path(tmp) / "reference"
        stage.mkdir()
        for name in RUNTIME_FILES:
            shutil.copy2(args.src / name, stage / name)
        bundle.write_manifest(
            stage,
            kind="reference",
            tag=args.tag,
            articles=meta.get("titles_fitted"),
            built_at=meta.get("built_at"),
            embedding_model=config.EMBEDDING_MODEL,
            embedding_model_revision=revision,
            versions=versions,
        )
        deterministic_tar(stage, asset)

    sha = bundle.sha256_file(asset)
    anchor = {
        "repo": REPO,
        "tag": args.tag,
        "asset": asset.name,
        "sha256": sha,
        "size": asset.stat().st_size,
        "articles": meta.get("titles_fitted"),
        "embedding_model_revision": revision,
        "versions": versions,
    }
    ANCHOR.write_text(json.dumps(anchor, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"\nWrote {asset} ({asset.stat().st_size / 1e6:.1f} MB, "
          f"{meta.get('titles_fitted')} articles)")
    print(f"sha256 {sha}")
    print(f"Updated {ANCHOR.relative_to(config.PROJECT_ROOT)} (commit it after uploading).")
    print("\nUpload it (gh: `conda install -c conda-forge gh`, then `gh auth login`):\n")
    print(f"  gh release create {args.tag} --repo {REPO} \\")
    print(f"      --title \"Reference space {version}\" --prerelease \\")
    print(f"      --notes \"UMAP reference space ({meta.get('titles_fitted')} articles), "
          f"sha256 {sha}\"")
    print(f"  gh release upload {args.tag} {asset} --repo {REPO}")
    print("\nThen check it round-trips:\n")
    print("  python scripts/fetch_reference_space.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
