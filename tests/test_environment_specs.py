"""The dev environment, the runtime spec, and the lock files must agree.

packaging/runtime.yml is what the installers ship; environment.yml is what
the app is developed and tested against. If they drift, the app gets
tested against packages the installer does not ship (or the reverse), and
the first sign is a user's crash. These checks make drift a test failure.
"""

import fnmatch
import re
from pathlib import Path

import pytest

import config

ROOT = Path(config.PROJECT_ROOT)
PACKAGING = ROOT / "packaging"
PLATFORMS = ("osx-64", "osx-arm64", "win-64")

yaml = pytest.importorskip("yaml")


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def conda_specs(env: dict) -> list[str]:
    return [d for d in env["dependencies"] if isinstance(d, str)]


def pip_specs(env: dict) -> list[str]:
    for d in env["dependencies"]:
        if isinstance(d, dict) and "pip" in d:
            return d["pip"]
    return []


RUNTIME = load(PACKAGING / "runtime.yml")
DEV = load(ROOT / "environment.yml")


def test_every_runtime_pin_is_in_the_dev_env_unchanged():
    missing = [s for s in conda_specs(RUNTIME) if s not in conda_specs(DEV)]
    assert missing == [], f"runtime entries missing or different in environment.yml: {missing}"


def test_channels_match():
    assert RUNTIME["channels"] == DEV["channels"]


def test_the_pip_pin_matches_the_wheel_lock_input():
    wanted = [line.strip() for line in
              (PACKAGING / "wheels" / "requirements.in").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
    assert pip_specs(DEV) == wanted


def test_runtime_spec_has_no_pip_section():
    """Constructor installs conda packages only; pip wheels go through
    packaging/wheels/ so they can be hash-locked per platform."""
    assert pip_specs(RUNTIME) == []


# ---------------------------------------------------------------------------
# The locks are not stale
# ---------------------------------------------------------------------------

def locked_packages(platform: str) -> dict[str, tuple[str, str]]:
    """name -> (version, build) from an explicit conda lock."""
    out = {}
    for line in (PACKAGING / "locks" / f"{platform}.txt").read_text().splitlines():
        if not line.startswith("http"):
            continue
        filename = line.split("#")[0].rsplit("/", 1)[-1]
        stem = re.sub(r"\.(conda|tar\.bz2)$", "", filename)
        name, version, build = stem.rsplit("-", 2)
        out[name] = (version, build)
    return out


def satisfies(spec: str, version: str, build: str) -> bool:
    """Enough of conda's "name=version[=build]" matching for these specs:
    "1.26.4" matches 1.26.4 (and 1.26.4.x), "1.5.*" is a prefix, and a
    build glob such as "cpu_*" must match the build string."""
    parts = spec.split("=")
    wanted_version = parts[1] if len(parts) > 1 else "*"
    wanted_build = parts[2] if len(parts) > 2 else "*"
    version_ok = (fnmatch.fnmatch(version, wanted_version)
                  or fnmatch.fnmatch(version, wanted_version + ".*"))
    return version_ok and fnmatch.fnmatch(build, wanted_build)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_each_lock_satisfies_the_runtime_spec(platform):
    """Catches a runtime.yml edit that was never re-locked."""
    locked = locked_packages(platform)
    for spec in conda_specs(RUNTIME):
        name = spec.split("=")[0]
        assert name in locked, f"{name} is not in the {platform} lock; re-run conda-lock"
        version, build = locked[name]
        assert satisfies(spec, version, build), (
            f"{platform} lock has {name} {version} {build}, which does not "
            f"satisfy {spec!r}; re-run conda-lock"
        )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_locks_ship_cpu_only_pytorch(platform):
    version, build = locked_packages(platform)["pytorch"]
    assert build.startswith("cpu_"), build
    assert not any(name.startswith(("cuda", "libcublas", "libcudnn"))
                   for name in locked_packages(platform))


@pytest.mark.parametrize("platform", PLATFORMS)
def test_locks_leave_out_the_dev_only_packages(platform):
    locked = locked_packages(platform)
    for name in ("pytest", "pandas", "pyarrow", "matplotlib-base"):
        assert name not in locked, f"{name} should not ship in the {platform} installer"


@pytest.mark.parametrize("platform", PLATFORMS)
def test_pip_and_conda_never_supply_the_same_package(platform):
    """Each package has exactly one owner, so pip never overwrites a conda
    package at install time."""
    norm = lambda n: re.sub(r"[-_.]+", "-", n).lower()  # noqa: E731
    conda = {norm(n) for n in locked_packages(platform)}
    text = (PACKAGING / "wheels" / f"requirements-{platform}.txt").read_text()
    pip = {norm(line.split("==")[0]) for line in text.splitlines()
           if line and not line[0].isspace() and not line.startswith("#")}
    assert "pywebview" in pip
    assert pip & conda == set()
    assert "--hash=sha256:" in text
