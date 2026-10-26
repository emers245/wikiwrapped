"""Lay out the app's own files as a conda package installs them.

    python packaging/stage_app.py                  # into $PREFIX (rattler-build)
    python packaging/stage_app.py --prefix DIR     # anywhere, to inspect

Run by packaging/recipe/recipe.yaml while building the `wikipedia-wrapped`
conda package; it reads the repo directly (from beside this file), so the
recipe needs no copy of the source. What it puts under <prefix>:

    share/wikipedia-wrapped/          the code folder: config.PROJECT_ROOT
                                      once installed
        launch.py                     what the shortcuts run (packaging/launch.py)
        config.py, app/, extract/     the app, without bytecode caches
        scripts/enrich_pending.py     the one script the app runs itself
                                      (the enrichment job, app/jobs.py)
    Menu/wikipedia-wrapped.json       the shortcut definition for menuinst,
                                      with the version filled in
    Menu/wikipedia-wrapped.icns/.ico  its icons (packaging/icon/make_icons.py)

The files are listed explicitly rather than "the repo minus some things",
so a new file in the checkout (notes, data, a token) can never end up in
an installer by accident.

Not in the package: the bundled model and reference space. They are large,
are not built from this repo, and reach the installer through constructor's
extra_files instead (packaging/installer/construct.yaml), into
share/wikipedia-wrapped/resources/, the folder config.installed_resource_dir()
looks for.
"""

from __future__ import annotations

import argparse
import ast
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CODE_DIR = Path("share") / "wikipedia-wrapped"
MENU_DIR = Path("Menu")
MENU_NAME = "wikipedia-wrapped"

# (source in the repo, destination under the code folder)
FILES = [
    ("packaging/launch.py", "launch.py"),
    ("config.py", "config.py"),
    ("scripts/enrich_pending.py", "scripts/enrich_pending.py"),
]
TREES = ["app", "extract"]
NEVER = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")

DEFAULT_ICONS = REPO / "packaging" / "build" / "icon"
MENU_TEMPLATE = REPO / "packaging" / "menu" / f"{MENU_NAME}.json"


def app_version() -> str:
    """config.APP_VERSION, read from the source without importing config
    (which needs the app's dependencies, absent from a build environment)."""
    tree = ast.parse((REPO / "config.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and getattr(node.targets[0], "id", None) == "APP_VERSION"):
            return ast.literal_eval(node.value)
    raise SystemExit("config.py does not set APP_VERSION")


def stage(prefix: Path, version: str, icons: Path = DEFAULT_ICONS) -> None:
    code = prefix / CODE_DIR
    for src, dest in FILES:
        (code / dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / src, code / dest)
    for tree in TREES:
        shutil.copytree(REPO / tree, code / tree, ignore=NEVER, dirs_exist_ok=True)

    menu = prefix / MENU_DIR
    menu.mkdir(parents=True, exist_ok=True)
    text = MENU_TEMPLATE.read_text(encoding="utf-8").replace("@VERSION@", version)
    (menu / f"{MENU_NAME}.json").write_text(text, encoding="utf-8")
    for ext in ("icns", "ico"):
        icon = icons / f"{MENU_NAME}.{ext}"
        if not icon.is_file():
            raise SystemExit(f"{icon} is missing: run packaging/icon/make_icons.py first")
        shutil.copy2(icon, menu / icon.name)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prefix", type=Path, default=os.environ.get("PREFIX"),
                        help="install tree to fill (default: $PREFIX, set by rattler-build)")
    parser.add_argument("--icons", type=Path, default=DEFAULT_ICONS)
    args = parser.parse_args(argv)
    if args.prefix is None:
        raise SystemExit("No --prefix given and $PREFIX is not set")

    version = app_version()
    # rattler-build passes the package version it was given; it must be the
    # app's own, or the package and the app inside it would disagree.
    package_version = os.environ.get("PKG_VERSION")
    if package_version and package_version != version:
        raise SystemExit(f"Package version {package_version} != config.APP_VERSION {version}")
    stage(Path(args.prefix), version, args.icons)
    print(f"Staged Wikipedia Wrapped {version} into {args.prefix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
