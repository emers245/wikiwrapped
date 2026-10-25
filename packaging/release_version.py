"""The version being built, and whether a git tag agrees with it.

    python packaging/release_version.py                  # prints the version
    python packaging/release_version.py --tag v0.1.0     # exits 1 unless they match
    python packaging/release_version.py --tag "$GITHUB_REF_NAME" --github-output

The version lives in one place, config.APP_VERSION. A release is tagged
v<version>, for example v0.1.0. A test build of the same version may carry
a pre-release suffix, v0.1.0-rc.1: the installers are still named 0.1.0,
and the draft release is marked as a pre-release. Any other tag is
refused, so a release can never ship installers built from a different
version than its tag says.

--github-output writes version, tag and prerelease to $GITHUB_OUTPUT for
the later jobs of .github/workflows/build-installers.yml.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage_app  # noqa: E402  (reads config.APP_VERSION without importing config)

TAG_PATTERN = re.compile(r"v(?P<version>\d+\.\d+\.\d+)(?P<suffix>-[0-9A-Za-z.]+)?")


def check_tag(tag: str, version: str) -> bool:
    """True for v<version> (a release) or v<version>-<suffix> (a test
    build); raises SystemExit for anything else."""
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        raise SystemExit(f"Tag {tag!r} is not v<major>.<minor>.<patch>[-<suffix>]")
    if match["version"] != version:
        raise SystemExit(
            f"Tag {tag} does not match config.APP_VERSION {version}: set "
            f"APP_VERSION = \"{match['version']}\" in config.py, or tag v{version}."
        )
    return match["suffix"] is None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tag", help="the git tag being released, if any")
    parser.add_argument("--github-output", action="store_true",
                        help="also write the results to $GITHUB_OUTPUT")
    args = parser.parse_args(argv)

    version = stage_app.app_version()
    release = check_tag(args.tag, version) if args.tag else False
    print(f"version {version}" + (f", tag {args.tag} ({'release' if release else 'test build'})"
                                  if args.tag else ""))
    if args.github_output:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as out:
            out.write(f"version={version}\n")
            out.write(f"tag={args.tag or ''}\n")
            out.write(f"prerelease={'false' if release else 'true'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
