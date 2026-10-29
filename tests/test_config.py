"""Path resolution and the project User-Agent (config.py).

Path functions are called directly rather than through the import-time
constants, because those were resolved once against the test override
(see conftest.py)."""

from pathlib import Path

import config
from app import localconfig


# ---------------------------------------------------------------------------
# User-Agent: identifies the project, never requires a personal address
# ---------------------------------------------------------------------------

def test_user_agent_names_the_project_and_version(monkeypatch):
    monkeypatch.delenv(config.CONTACT_EMAIL_ENV_VAR, raising=False)
    assert config.get_user_agent() == (
        f"WikiWrapped/{config.APP_VERSION} "
        "(https://github.com/emers245/wikiwrapped)"
    )


def test_user_agent_appends_an_optional_email(monkeypatch):
    monkeypatch.setenv(config.CONTACT_EMAIL_ENV_VAR, "dev@example.com")
    assert config.get_user_agent().endswith(
        "(https://github.com/emers245/wikiwrapped; dev@example.com)"
    )


def test_session_carries_the_user_agent(monkeypatch):
    from app import wiki_http

    monkeypatch.delenv(config.CONTACT_EMAIL_ENV_VAR, raising=False)
    session = wiki_http.make_session()
    assert session.headers["User-Agent"] == config.get_user_agent()
    assert session.headers["Api-User-Agent"] == config.get_user_agent()


# ---------------------------------------------------------------------------
# User data vs app resources
# ---------------------------------------------------------------------------

def test_user_data_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(config.USER_DATA_ENV_VAR, str(tmp_path / "scratch"))
    assert config.user_data_dir() == (tmp_path / "scratch").resolve()


def test_user_data_defaults_to_the_platform_location(monkeypatch):
    import platformdirs

    calls = {}

    def fake(appname, appauthor=None, roaming=False):
        calls.update(appname=appname, appauthor=appauthor, roaming=roaming)
        return "/platform/default/WikiWrapped"

    monkeypatch.delenv(config.USER_DATA_ENV_VAR, raising=False)
    monkeypatch.setattr(platformdirs, "user_data_dir", fake)
    assert config.user_data_dir() == Path("/platform/default/WikiWrapped")
    # appauthor=False keeps Windows from adding a company folder, and
    # roaming=True picks %APPDATA% rather than %LOCALAPPDATA%.
    assert calls == {"appname": "WikiWrapped", "appauthor": False, "roaming": True}


def test_the_test_suite_never_points_at_real_user_data():
    """conftest.py's guarantee, restated where someone will read it."""
    for path in (config.DB_PATH, config.VISIT_LOG_PATH, config.SYNC_STATE_PATH,
                 config.LOCAL_CONFIG_PATH, config.TOKEN_DIR, config.LOG_DIR):
        assert Path(path).resolve().is_relative_to(Path(config.USER_DATA_DIR))
    assert "wikiwrapped-tests-" in str(config.USER_DATA_DIR)


def test_user_data_never_lives_in_the_program_folder():
    root = Path(config.PROJECT_ROOT).resolve()
    for path in (config.DB_PATH, config.VISIT_LOG_PATH, config.SYNC_STATE_PATH,
                 config.LOCAL_CONFIG_PATH, config.TOKEN_DIR, config.LOG_DIR,
                 config.CACHE_DIR):
        assert not Path(path).resolve().is_relative_to(root), path


def test_resource_order_env_then_installed_then_repo(monkeypatch, tmp_path):
    monkeypatch.delenv(config.RESOURCE_ENV_VAR, raising=False)
    assert config.resource_dir() == Path(config.PROJECT_ROOT) / "data"

    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setattr(config, "installed_resource_dir", lambda: installed)
    assert config.resource_dir() == installed

    monkeypatch.setenv(config.RESOURCE_ENV_VAR, str(tmp_path / "override"))
    assert config.resource_dir() == (tmp_path / "override").resolve()


def test_missing_installed_location_falls_through_to_the_repo(monkeypatch, tmp_path):
    monkeypatch.delenv(config.RESOURCE_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "installed_resource_dir", lambda: tmp_path / "absent")
    assert config.resource_dir() == Path(config.PROJECT_ROOT) / "data"


def test_config_never_creates_directories():
    """Resolving paths must not be a side effect of importing config.

    The directories themselves cannot be checked here, because other tests
    legitimately write into the scratch dir; so the guarantee is checked
    where it lives, in config's own source: it never creates anything.
    """
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert "mkdir" not in source and "makedirs" not in source


# ---------------------------------------------------------------------------
# Client secret lookup moved from the repo root to the user-data dir
# ---------------------------------------------------------------------------

def test_client_secret_is_found_in_the_user_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    settings = localconfig.load(tmp_path / "absent.toml")
    assert localconfig.client_secret_path(settings) is None

    secret = tmp_path / "client_secret_123.json"
    secret.write_text("{}", encoding="utf-8")
    assert localconfig.client_secret_path(settings) == secret


def test_relative_client_secret_resolves_against_user_data(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    (tmp_path / "mine.json").write_text("{}", encoding="utf-8")
    settings = localconfig.load(tmp_path / "absent.toml")
    settings["takeout"]["client_secret"] = "mine.json"
    assert localconfig.client_secret_path(settings) == tmp_path / "mine.json"
