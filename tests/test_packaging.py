"""The installer's pieces that can be checked without building one.

Building and installing an installer is done by packaging/build_installer.py
and verified by the installed app's --self-test; these tests cover what
goes into the app package, and the values that several packaging files
must agree on (a mismatch there breaks an install silently: a taskbar
showing "Python", an upgrade that never removes the old version)."""

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import config

ROOT = config.PROJECT_ROOT
PACKAGING = ROOT / "packaging"
INSTALLER = PACKAGING / "installer"


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage_app = load(PACKAGING / "stage_app.py")


def constant(path: Path, name: str):
    """A module-level constant, read without running the file."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == name:
            return ast.literal_eval(node.value)
    raise AssertionError(f"{path.name} does not set {name}")


@pytest.fixture(scope="module")
def staged(tmp_path_factory):
    prefix = tmp_path_factory.mktemp("prefix")
    icons = tmp_path_factory.mktemp("icons")
    for ext in ("icns", "ico"):
        (icons / f"wikiwrapped.{ext}").write_bytes(b"icon")
    stage_app.stage(prefix, "9.8.7", icons)
    return prefix


# ---------------------------------------------------------------------------
# What the app package contains
# ---------------------------------------------------------------------------

def test_the_version_comes_from_config():
    assert stage_app.app_version() == config.APP_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", config.APP_VERSION), "tags are v<major.minor.patch>"


def test_the_package_holds_the_app_and_nothing_else(staged):
    code = staged / "share" / "wikiwrapped"
    for rel in ("launch.py", "config.py", "scripts/enrich_pending.py",
                "app/launcher.py", "app/selftest.py", "app/bundle_fixture.json",
                "app/static/index.html", "app/static/help.html",
                "extract/extract_history.py"):
        assert (code / rel).is_file(), rel
    top = {p.name for p in code.iterdir()}
    assert top == {"launch.py", "config.py", "scripts", "app", "extract"}
    assert [p.name for p in (code / "scripts").iterdir()] == ["enrich_pending.py"]
    assert not list(staged.rglob("__pycache__")) and not list(staged.rglob("*.pyc"))
    assert not (code / "resources").exists(), "resources come from the installer"


def test_every_help_image_is_packaged(staged):
    help_html = (ROOT / "app" / "static" / "help.html").read_text(encoding="utf-8")
    static = staged / "share" / "wikiwrapped" / "app" / "static"
    for src in re.findall(r'<img[^>]*\bsrc="([^"]+)"', help_html):
        assert (static / src).is_file(), src


def test_the_menu_entry_gets_the_version_and_its_icons(staged):
    menu = staged / "Menu"
    data = json.loads((menu / "wikiwrapped.json").read_text(encoding="utf-8"))
    item = data["menu_items"][0]
    assert item["platforms"]["osx"]["CFBundleVersion"] == "9.8.7"
    assert "@VERSION@" not in (menu / "wikiwrapped.json").read_text(encoding="utf-8")
    assert (menu / "wikiwrapped.icns").is_file() and (menu / "wikiwrapped.ico").is_file()
    # Every command runs the launch script the package installs.
    commands = [item["command"], *(p["command"] for p in item["platforms"].values())]
    assert all(c[-1] == "{{ PREFIX }}/share/wikiwrapped/launch.py" for c in commands)
    # No conda activation: it would need a console window on Windows.
    assert item["activate"] is False and item["terminal"] is False


def test_the_packaged_app_runs_from_its_own_folder(staged, tmp_path):
    """Imports and starts from the staged tree alone, as when installed:
    nothing in the app may depend on a repo file that is not packaged."""
    code = staged / "share" / "wikiwrapped"
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           config.USER_DATA_ENV_VAR: str(tmp_path / "userdata")}
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(code / "launch.py"), "--help"], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "--self-test" in result.stdout
    imports = subprocess.run(
        [sys.executable, "-c",
         "import config, app.api, app.sync, app.selftest, app.umap_space; "
         "print(config.PROJECT_ROOT)"],
        cwd=code, env=env, capture_output=True, text=True, timeout=120,
    )
    assert imports.returncode == 0, imports.stderr
    assert Path(imports.stdout.strip()) == code.resolve()
    assert not list(code.rglob("__pycache__")), "launch.py must not write bytecode"


# ---------------------------------------------------------------------------
# Values several files must agree on
# ---------------------------------------------------------------------------

MENU = json.loads((PACKAGING / "menu" / "wikiwrapped.json").read_text(encoding="utf-8"))
MENU_ITEM = MENU["menu_items"][0]


def test_windows_taskbar_id_matches_between_shortcut_and_launcher():
    assert (MENU_ITEM["platforms"]["win"]["app_user_model_id"]
            == constant(PACKAGING / "launch.py", "APP_USER_MODEL_ID"))


def test_macos_scripts_only_touch_our_own_shortcut():
    bundle_id = MENU_ITEM["platforms"]["osx"]["CFBundleIdentifier"]
    for script in ("pre_install.sh", "uninstall.sh"):
        text = (INSTALLER / "macos" / script).read_text(encoding="utf-8")
        assert f'OUR_BUNDLE_ID="{bundle_id}"' in text, script
        assert f'"$HOME/Applications/{MENU_ITEM["name"]}.app"' in text, script
    assert len(MENU_ITEM["platforms"]["osx"]["CFBundleName"]) <= 16, "menuinst's limit"


def test_install_folders_match_what_the_upgrade_and_uninstall_scripts_expect():
    construct = (INSTALLER / "construct.yaml").read_text(encoding="utf-8")
    # macOS: versions live in ~/Library/WikiWrapped-App/v<version>.
    assert "default_location_pkg: Library/WikiWrapped-App  # [osx]" in construct
    assert 'pkg_name: v{{ environ["WW_VERSION"] }}  # [osx]' in construct
    post = (INSTALLER / "macos" / "post_install.sh").read_text(encoding="utf-8")
    assert '"$INSTALL_ROOT"/v*' in post
    assert "WikiWrapped-App/uninstall.sh" in (INSTALLER / "macos" / "uninstall.sh").read_text()
    # Windows: an older version is found by its "Installed apps" name.
    name = re.search(r"^uninstall_name: (.+?)  # \[win\]$", construct, re.M).group(1)
    assert name.startswith("WikiWrapped ")
    pre = (INSTALLER / "windows" / "pre_install.bat").read_text(encoding="utf-8")
    assert "-notlike 'WikiWrapped *'" in pre


def test_only_selector_lines_end_in_brackets():
    """constructor reads any line ending in [...] as a platform selector."""
    for number, line in enumerate((INSTALLER / "construct.yaml").read_text().splitlines(), 1):
        if line.rstrip().endswith("]") and not line.lstrip().startswith("#"):
            assert re.search(r"#\s*\[(osx|win|arm64|osx and x86_64)\]$", line), (number, line)


def test_packaging_files_are_plain_ascii():
    for path in [*PACKAGING.rglob("*.py"), *PACKAGING.rglob("*.sh"),
                 *PACKAGING.rglob("*.bat"), *PACKAGING.rglob("*.yaml"),
                 *PACKAGING.rglob("*.txt"), PACKAGING / "menu" / "wikiwrapped.json"]:
        if "build" in path.relative_to(PACKAGING).parts:
            continue
        path.read_bytes().decode("ascii")


# ---------------------------------------------------------------------------
# The release workflow (.github/workflows/build-installers.yml)
# ---------------------------------------------------------------------------

release_version = load(PACKAGING / "release_version.py")
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "build-installers.yml"


def test_a_release_tag_must_name_the_code_version():
    assert release_version.check_tag("v1.2.3", "1.2.3") is True
    assert release_version.check_tag("v1.2.3-rc.1", "1.2.3") is False   # a test build
    for bad in ("v1.2.4", "1.2.3", "v1.2", "v1.2.3rc1", "release-1.2.3"):
        with pytest.raises(SystemExit):
            release_version.check_tag(bad, "1.2.3")


def test_the_version_check_writes_what_the_later_jobs_read(tmp_path, monkeypatch):
    output = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert release_version.main(["--tag", f"v{config.APP_VERSION}", "--github-output"]) == 0
    assert output.read_text().splitlines() == [
        f"version={config.APP_VERSION}", f"tag=v{config.APP_VERSION}", "prerelease=false"]


def test_the_workflow_builds_every_locked_platform_with_matching_names():
    import yaml

    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    matrix = workflow["jobs"]["build"]["strategy"]["matrix"]["include"]
    locks = {p.stem for p in (PACKAGING / "locks").glob("*.txt")}
    assert {entry["platform"] for entry in matrix} == locks
    construct = (INSTALLER / "construct.yaml").read_text(encoding="utf-8")
    for entry in matrix:
        assert re.search(rf"^installer_filename: .*\.{entry['installer']}  #", construct, re.M)
    # The steps look for the install where construct.yaml puts it.
    steps = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "Library/WikiWrapped-App" in steps and "'WikiWrapped-App'" in steps
    assert "Uninstall-WikiWrapped.exe" in steps
    assert re.search(r"^name: WikiWrapped$", construct, re.M)
    # Only the release job may write to the repository.
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["release"]["permissions"] == {"contents": "write"}


def test_batch_files_keep_windows_line_endings():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert re.search(r"^\*\.bat\s+text eol=crlf$", attributes, re.M)
    assert re.search(r"^\*\.sh\s+text eol=lf$", attributes, re.M)
