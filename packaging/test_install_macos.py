"""Install a built .pkg into a scratch folder, test it, and uninstall it.

    python packaging/test_install_macos.py packaging/build/dist/<installer>.pkg --scratch DIR

For checking an installer on the build Mac without touching your own home
folder. macOS's `installer` command can only put a "just me" package into
the real home folder, so this does by hand what Installer does: expands
the .pkg and runs each part in order (pre-install script, files,
post-install script), with HOME pointing at DIR/home. The GitHub runners
(Milestone 4) use the real `installer` instead, on a throwaway machine.

What it checks:

1. The install: the shortcut app exists and is ours, the wheels were
   installed and removed, the app was precompiled, the uninstaller sits
   at the install root.
2. --self-test, run twice: with the installed Python, and through the
   shortcut's in-bundle Python (the one the Dock icon depends on).
3. An upgrade: a fake older version and its shortcut are put in place
   first; afterwards both must be gone.
4. The uninstall: uninstall.sh removes the program and the shortcut and
   leaves the user-data folder alone.

The app's user data go to DIR/userdata throughout, and installer progress
notifications are printed here rather than shown on screen.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

APP_NAME = "Wikipedia Wrapped.app"
BUNDLE_ID = "io.github.emers245.wikipediawrapped"
INSTALL_ROOT = Path("Library") / "WikipediaWrapped-App"


def sh(cmd, **kwargs) -> subprocess.CompletedProcess:
    print("    $ " + " ".join(map(str, cmd)), flush=True)
    return subprocess.run([str(c) for c in cmd], **kwargs)


def expand(pkg: Path, dest: Path) -> list[Path]:
    """The sub-packages, in the order Installer runs them."""
    sh(["pkgutil", "--expand-full", pkg, dest], check=True)
    distribution = ET.parse(dest / "Distribution").getroot()
    order = [ref.text.lstrip("#") for ref in distribution.iter("pkg-ref") if ref.text]
    return [dest / name for name in dict.fromkeys(order)]


def script_env(home: Path, fakebin: Path) -> dict:
    """What Installer gives package scripts: a clean environment, not ours
    (a conda environment's variables would leak into conda-standalone)."""
    return {
        "HOME": str(home), "USER": os.environ["USER"], "LOGNAME": os.environ["USER"],
        "PATH": f"{fakebin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "COMMAND_LINE_INSTALL": "1",
    }


def install(pkg: Path, scratch: Path, env: dict) -> None:
    parts = expand(pkg, scratch / "expanded")
    target = Path(env["HOME"]) / INSTALL_ROOT
    target.mkdir(parents=True, exist_ok=True)
    for part in parts:
        print(f"  {part.name}")
        args = [pkg, target, "/", "/"]
        pre = part / "Scripts" / "preinstall"
        if pre.exists():
            sh([pre, *args], env=env, check=True)
        payload = part / "Payload"
        if payload.is_dir():
            shutil.copytree(payload, target, symlinks=True, dirs_exist_ok=True)
        post = part / "Scripts" / "postinstall"
        if post.exists():
            sh([post, *args], env=env, check=True)


def fake_previous_version(home: Path) -> tuple[Path, Path]:
    old = home / INSTALL_ROOT / "v0.0.1"
    (old / "conda-meta").mkdir(parents=True)
    (old / "conda-meta" / "wikipedia-wrapped-0.0.1-h0_0.json").write_text("{}")
    app = home / "Applications" / APP_NAME
    (app / "Contents").mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": BUNDLE_ID}, handle)
    return old, app


def check(condition: bool, message: str, failures: list) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {message}")
    if not condition:
        failures.append(message)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pkg", type=Path)
    parser.add_argument("--scratch", type=Path, required=True,
                        help="an empty folder to install into (it is cleared first)")
    parser.add_argument("--keep", action="store_true",
                        help="stop after installing, without self-tests or uninstalling, "
                             "to look around")
    args = parser.parse_args(argv)
    pkg = args.pkg.resolve()
    scratch = args.scratch.resolve()
    shutil.rmtree(scratch, ignore_errors=True)
    home, userdata, fakebin = scratch / "home", scratch / "userdata", scratch / "fakebin"
    for folder in (home, fakebin):
        folder.mkdir(parents=True)
    # Progress notifications go to the terminal instead of the screen.
    (fakebin / "osascript").write_text('#!/bin/sh\ncat > /dev/null\necho "(notification)"\n')
    (fakebin / "osascript").chmod(0o755)
    env = script_env(home, fakebin)
    failures: list[str] = []

    print("\n== Installing over a fake previous version")
    old_version, app = fake_previous_version(home)
    install(pkg, scratch, env)

    version = re.search(r"WikipediaWrapped-(\d+\.\d+\.\d+)-", pkg.name).group(1)
    prefix = home / INSTALL_ROOT / f"v{version}"
    code = prefix / "share" / "wikipedia-wrapped"
    print("\n== Checking the installation")
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes()) if app.exists() else {}
    check(info.get("CFBundleIdentifier") == BUNDLE_ID, f"shortcut {app} is ours", failures)
    check(info.get("CFBundleVersion") == version, "shortcut carries the version", failures)
    check(not old_version.exists(), "the previous version was removed", failures)
    check(not (prefix / "wheels").exists(), "the wheels were installed and removed", failures)
    check(any(code.rglob("*.cpython-311.pyc")), "the app was precompiled", failures)
    check((home / INSTALL_ROOT / "uninstall.sh").is_file(), "uninstaller at the install root", failures)
    check(not (prefix / "pkgs").exists() or not any((prefix / "pkgs").glob("*.conda")),
          "the package cache was cleaned up", failures)
    check(not any(prefix.rglob("*.nbi")), "no numba cache written into the install", failures)
    sizes = subprocess.run(["du", "-sh", str(prefix)], capture_output=True, text=True).stdout
    print(f"  installed size: {sizes.split()[0] if sizes else '?'}")

    # How long a first launch waits on imports before its window can open.
    # Measured before anything else runs the installed Python, so this is
    # the first load after the installer's own warm-up.
    started = time.monotonic()
    sh([prefix / "bin" / "python", "-B", "-c",
        "import app.launcher, app.api, webview, AppKit, Foundation, WebKit"],
       cwd=code, env={**env, "WIKIPEDIA_WRAPPED_DATA_DIR": str(userdata)}, check=True)
    print(f"  first launch's imports took {time.monotonic() - started:.1f} s")

    if args.keep:
        print(f"\nInstalled into {prefix} (kept; HOME for it is {home}).")
        return 1 if failures else 0

    run_env = {**env, "WIKIPEDIA_WRAPPED_DATA_DIR": str(userdata)}
    for label, python in (("installed Python", prefix / "bin" / "python"),
                          ("the shortcut's in-bundle Python", app / "Contents" / "MacOS" / "python")):
        print(f"\n== Self-test with {label}")
        result = sh([python, code / "launch.py", "--self-test"], env=run_env)
        check(result.returncode == 0, f"self-test passed ({label})", failures)
    check(not userdata.exists(), "the self-test created nothing in the user-data folder", failures)

    print("\n== Uninstalling")
    userdata.mkdir()
    (userdata / "keep.txt").write_text("user data")
    result = sh(["bash", home / INSTALL_ROOT / "uninstall.sh"], env=run_env)
    check(result.returncode == 0, "uninstall.sh succeeded", failures)
    check(not (home / INSTALL_ROOT).exists(), "the program was removed", failures)
    check(not app.exists(), "the shortcut was removed", failures)
    check((userdata / "keep.txt").exists(), "user data were kept", failures)

    print(f"\n{'All checks passed.' if not failures else f'{len(failures)} check(s) FAILED.'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
