"""app.setup_service and the /settings endpoints built on it, plus the CLI.

The service is where every setup rule lives, so most tests are here; the
endpoint tests check the wiring (status codes, nothing saved on a rejected
change) and the localhost guard. Google is never contacted:
drive.authorize_account is replaced by a fake."""

import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import config
from app import api, drive, jobs, localconfig, setup_service, sync

LOCAL = "http://127.0.0.1"


@pytest.fixture
def userdata(tmp_path, monkeypatch):
    """Every user-data path pointed at a fresh directory."""
    monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "LOCAL_CONFIG_PATH", tmp_path / "config.local.toml")
    monkeypatch.setattr(config, "TOKEN_DIR", tmp_path / "tokens")
    monkeypatch.setattr(config, "SYNC_STATE_PATH", tmp_path / "data" / "sync_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    return tmp_path


@pytest.fixture
def fake_chrome(monkeypatch):
    """Two Chrome profiles, one readable with 3 Wikipedia visits."""
    profiles = [
        {"directory": "Default", "display_name": "Person 1", "account": "",
         "path": "/nowhere/Default/History"},
        {"directory": "Profile 1", "display_name": "Work",
         "account": "work@example.com", "path": "/nowhere/Profile 1/History"},
    ]
    monkeypatch.setattr(setup_service.extract_history, "find_chrome_profiles",
                        lambda only=None: profiles)

    def load(path):
        if "Profile 1" in str(path):
            raise OSError("locked")
        return [("https://en.wikipedia.org/wiki/Cat", "Cat", 1.7e9 + i) for i in range(3)]

    monkeypatch.setattr(setup_service.extract_history, "load_chrome_db", load)
    return profiles


def takeout_zip(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Takeout/Chrome/History.json",
                         json.dumps({"Browser History": []}))
    return path


def write_secret(path, kind="installed"):
    path.write_text(json.dumps({kind: {
        "client_id": "123.apps.googleusercontent.com",
        "client_secret": "not-really-secret",
        "redirect_uris": ["http://localhost"],
    }}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Reading state
# ---------------------------------------------------------------------------

def test_first_run_is_the_absence_of_a_settings_file(userdata):
    assert setup_service.is_first_run()
    localconfig.save(localconfig.load())
    assert not setup_service.is_first_run()


def test_profiles_report_visits_and_survive_an_unreadable_one(userdata, fake_chrome):
    found = setup_service.chrome_profiles(localconfig.load())
    assert [(p["directory"], p["wikipedia_visits"], p["selected"]) for p in found] == [
        ("Default", 3, True), ("Profile 1", None, True),
    ]


def test_profile_selection_is_reflected(userdata, fake_chrome):
    settings = setup_service.set_profiles(localconfig.load(), ["Profile 1"])
    found = setup_service.chrome_profiles(settings, count_visits=False)
    assert [p["selected"] for p in found] == [False, True]


def test_inspect_folder_counts_takeout_zips(tmp_path):
    takeout_zip(tmp_path / "t" / "takeout-1.zip")
    takeout_zip(tmp_path / "t" / "sub" / "takeout-2.zip")
    found = setup_service.inspect_folder(tmp_path / "t")
    assert found["status"] == "ok" and found["zips_found"] == 2
    assert "2 Takeout archives found" in found["message"]


def test_inspect_folder_explains_an_empty_or_missing_folder(tmp_path):
    (tmp_path / "empty").mkdir()
    empty = setup_service.inspect_folder(tmp_path / "empty")
    assert empty["status"] == "ok" and "not arrived" in empty["message"]
    missing = setup_service.inspect_folder(tmp_path / "gone")
    assert missing["status"] == "missing"
    assert setup_service.inspect_folder("")["status"] == "missing"


def test_status_flags_accounts_that_need_signing_in_again(userdata):
    settings = localconfig.load()
    localconfig.add_account(settings, "ok@example.com")
    localconfig.add_account(settings, "stale@example.com")
    localconfig.add_account(settings, "notoken@example.com")
    for email in ("ok@example.com", "stale@example.com"):
        localconfig.token_path(email).parent.mkdir(parents=True, exist_ok=True)
        localconfig.token_path(email).write_text("{}", encoding="utf-8")
    sync.save_state({"reauthorize": ["stale@example.com"], "errors": ["x"]})

    info = setup_service.status(settings)
    flags = {a["email"]: a["needs_reauthorization"] for a in info["takeout"]["accounts"]}
    assert flags == {"ok@example.com": False, "stale@example.com": True,
                     "notoken@example.com": True}
    assert info["last_sync"]["reauthorize"] == ["stale@example.com"]
    assert info["paths"]["user_data"] == str(userdata)


def test_drive_on_without_accounts_is_warned_about(userdata):
    settings = setup_service.set_drive(localconfig.load(), True)
    assert "no Google account" in setup_service.incomplete_warning(settings)
    assert setup_service.incomplete_warning(localconfig.load()) is None


# ---------------------------------------------------------------------------
# Changing settings
# ---------------------------------------------------------------------------

def test_profile_choices_all_some_none(userdata):
    settings = localconfig.load()
    setup_service.set_profiles(settings, None)
    assert localconfig.local_enabled(settings) and localconfig.chrome_profiles(settings) == []
    setup_service.set_profiles(settings, ["Profile 1"])
    assert localconfig.chrome_profiles(settings) == ["Profile 1"]
    # Unticking everything must not store [] (which means "all").
    setup_service.set_profiles(settings, [])
    assert localconfig.local_enabled(settings) is False


def test_turning_local_chrome_off_skips_it_in_sync(userdata, tmp_path, monkeypatch):
    def must_not_run(**kwargs):
        raise AssertionError("local Chrome was read although it is off")

    monkeypatch.setattr(sync, "sync_local_chrome", must_not_run)
    settings = setup_service.set_profiles(localconfig.load(), [])
    report = sync.run(db_path=tmp_path / "app.db", settings=settings,
                      log_path=tmp_path / "visits.jsonl", force=True)
    assert report["local"] == {"skipped": "Reading local Chrome history is turned off"}


def test_adding_a_missing_folder_is_refused(userdata, tmp_path):
    with pytest.raises(setup_service.SetupError, match="not found"):
        setup_service.add_folder(localconfig.load(), tmp_path / "nope")


def test_an_existing_folder_may_be_temporarily_unreachable(userdata, tmp_path):
    """A watched folder on an unplugged drive must not block saving."""
    (tmp_path / "usb").mkdir()
    settings = setup_service.add_folder(localconfig.load(), tmp_path / "usb", "USB")
    (tmp_path / "usb").rmdir()
    setup_service.set_folders(settings, [
        {"path": str(tmp_path / "usb"), "label": "Still USB"}
    ])
    assert localconfig.watched_folders(settings)[0]["label"] == "Still USB"
    with pytest.raises(setup_service.SetupError):
        setup_service.set_folders(settings, [{"path": str(tmp_path / "new-missing")}])


def test_sync_interval_is_validated(userdata):
    settings = localconfig.load()
    setup_service.set_sync(settings, False, "12")
    assert localconfig.min_sync_interval_hours(settings) == 12
    assert localconfig.sync_on_launch(settings) is False
    for bad in ("soon", -1, 10_000):
        with pytest.raises(setup_service.SetupError):
            setup_service.set_sync(settings, True, bad)


def test_apply_takes_a_whole_settings_screen(userdata, tmp_path):
    (tmp_path / "t").mkdir()
    settings = setup_service.apply(localconfig.load(), {
        "local": {"enabled": True, "chrome_profiles": ["Default"]},
        "watched_folders": [{"path": str(tmp_path / "t"), "label": "Personal"}],
        "sync": {"on_launch": True, "min_interval_hours": 6},
        "takeout": {"enabled": False, "drive_folder": "Takeout"},
    })
    assert localconfig.chrome_profiles(settings) == ["Default"]
    assert localconfig.watched_folders(settings)[0]["label"] == "Personal"
    assert localconfig.min_sync_interval_hours(settings) == 6
    assert localconfig.drive_folder_id(settings) == "Takeout"


# ---------------------------------------------------------------------------
# Advanced: client secret and accounts
# ---------------------------------------------------------------------------

def test_client_secret_is_validated_and_copied_privately(userdata, tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    source = write_secret(downloads / "client_secret_123.json")

    settings = setup_service.import_client_secret(localconfig.load(), source)

    copy = userdata / setup_service.CLIENT_SECRET_NAME
    assert json.loads(copy.read_text()) == json.loads(source.read_text())
    assert copy.stat().st_mode & 0o777 == 0o600
    assert localconfig.client_secret_path(settings) == copy
    assert source.exists()          # the download is left where it was


def test_web_clients_and_other_json_are_rejected(userdata, tmp_path):
    with pytest.raises(setup_service.SetupError, match="Desktop app"):
        setup_service.import_client_secret(
            localconfig.load(), write_secret(tmp_path / "web.json", kind="web"))
    (tmp_path / "other.json").write_text('{"hello": 1}', encoding="utf-8")
    with pytest.raises(setup_service.SetupError, match="does not look like"):
        setup_service.import_client_secret(localconfig.load(), tmp_path / "other.json")
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(setup_service.SetupError, match="not a readable JSON"):
        setup_service.import_client_secret(localconfig.load(), tmp_path / "broken.json")
    with pytest.raises(setup_service.SetupError, match="not found"):
        setup_service.import_client_secret(localconfig.load(), tmp_path / "absent.json")


def test_authorize_adds_the_account_and_turns_drive_on(userdata, tmp_path, monkeypatch):
    if not drive.libraries_available():
        pytest.skip("google libraries not installed")
    settings = setup_service.import_client_secret(
        localconfig.load(), write_secret(tmp_path / "s.json"))
    seen = {}

    def fake_authorize(settings, email_hint=None, timeout_seconds=None):
        seen["timeout"] = timeout_seconds
        return "me@example.com"

    monkeypatch.setattr(drive, "authorize_account", fake_authorize)
    assert setup_service.authorize(settings) == "me@example.com"
    assert localconfig.account_emails(settings) == ["me@example.com"]
    assert localconfig.takeout_enabled(settings)
    assert seen["timeout"] == setup_service.AUTHORIZE_TIMEOUT_S


def test_authorize_needs_a_client_secret_first(userdata):
    if not drive.libraries_available():
        pytest.skip("google libraries not installed")
    with pytest.raises(setup_service.SetupError, match="client secret"):
        setup_service.authorize(localconfig.load())


def test_a_failed_sign_in_is_a_setup_error(userdata, tmp_path, monkeypatch):
    if not drive.libraries_available():
        pytest.skip("google libraries not installed")
    settings = setup_service.import_client_secret(
        localconfig.load(), write_secret(tmp_path / "s.json"))

    def refused(settings, email_hint=None, timeout_seconds=None):
        raise drive.DriveError("Authorization was not completed in the browser")

    monkeypatch.setattr(drive, "authorize_account", refused)
    with pytest.raises(setup_service.SetupError, match="not completed"):
        setup_service.authorize(settings)
    assert localconfig.account_emails(settings) == []


def test_removing_an_account_deletes_its_token(userdata):
    settings = localconfig.load()
    localconfig.add_account(settings, "me@example.com")
    token = localconfig.token_path("me@example.com")
    token.parent.mkdir(parents=True)
    token.write_text("{}", encoding="utf-8")
    setup_service.remove_account(settings, "me@example.com")
    assert localconfig.account_emails(settings) == [] and not token.exists()


# ---------------------------------------------------------------------------
# The /settings endpoints
# ---------------------------------------------------------------------------

@pytest.fixture
def client(userdata, tmp_path):
    app = api.create_app(db_path=tmp_path / "app.db",
                         reference_dir=tmp_path / "none",
                         job_runner=jobs.JobRunner(cwd=tmp_path))
    with TestClient(app, base_url=LOCAL) as test_client:
        yield test_client


def test_get_settings_on_first_run(client):
    info = client.get("/settings").json()
    assert info["first_run"] is True
    assert info["sync"]["on_launch"] is True


def test_put_settings_saves_and_ends_first_run(client, tmp_path):
    (tmp_path / "t").mkdir()
    response = client.put("/settings", json={
        "watched_folders": [{"path": str(tmp_path / "t"), "label": "Personal"}],
        "sync": {"on_launch": True, "min_interval_hours": 12},
    })
    assert response.status_code == 200
    assert response.json()["first_run"] is False
    saved = localconfig.load()
    assert localconfig.watched_folders(saved)[0]["label"] == "Personal"
    assert localconfig.min_sync_interval_hours(saved) == 12


def test_a_rejected_change_writes_nothing(client, tmp_path):
    response = client.put("/settings", json={
        "watched_folders": [{"path": str(tmp_path / "missing")}],
    })
    assert response.status_code == 400
    assert "not found" in response.json()["detail"]
    assert not config.LOCAL_CONFIG_PATH.exists()


def test_folder_inspection_endpoint(client, tmp_path):
    takeout_zip(tmp_path / "t" / "takeout-1.zip")
    found = client.post("/settings/folders/inspect",
                        json={"path": str(tmp_path / "t")}).json()
    assert found["zips_found"] == 1


def test_client_secret_endpoint_reports_problems_as_400(client, tmp_path):
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    bad = client.post("/settings/client-secret", json={"path": str(tmp_path / "x.json")})
    assert bad.status_code == 400
    good = client.post("/settings/client-secret",
                       json={"path": str(write_secret(tmp_path / "s.json"))})
    assert good.status_code == 200
    assert good.json()["client_secret"] == setup_service.CLIENT_SECRET_NAME


def test_authorize_endpoint_keeps_changes_made_while_waiting(client, tmp_path, monkeypatch):
    """A sign-in can take minutes; a Settings save during it must survive."""
    if not drive.libraries_available():
        pytest.skip("google libraries not installed")
    client.post("/settings/client-secret",
                json={"path": str(write_secret(tmp_path / "s.json"))})

    def slow_sign_in(settings, email_hint=None, timeout_seconds=None):
        # Meanwhile, the user changed a setting in another request.
        changed = localconfig.load()
        localconfig.set_sync_preferences(changed, False, 48)
        localconfig.save(changed)
        return "me@example.com"

    monkeypatch.setattr(drive, "authorize_account", slow_sign_in)
    response = client.post("/settings/accounts/authorize", json={})
    assert response.status_code == 200 and response.json()["email"] == "me@example.com"
    saved = localconfig.load()
    assert localconfig.account_emails(saved) == ["me@example.com"]
    assert localconfig.min_sync_interval_hours(saved) == 48


def test_delete_account_endpoint(client):
    settings = localconfig.load()
    localconfig.add_account(settings, "me@example.com")
    localconfig.save(settings)
    response = client.delete("/settings/accounts/me@example.com")
    assert response.status_code == 200 and response.json()["accounts"] == []


# ---------------------------------------------------------------------------
# The localhost guard
# ---------------------------------------------------------------------------

def test_foreign_host_names_are_refused(userdata, tmp_path):
    """DNS rebinding: a site's own name pointed at 127.0.0.1."""
    app = api.create_app(db_path=tmp_path / "app.db", reference_dir=tmp_path / "none")
    with TestClient(app, base_url="http://evil.example:48213") as foreign:
        assert foreign.get("/health").status_code == 403
        assert foreign.get("/dashboard").status_code == 403


def test_cross_site_writes_are_refused(client, tmp_path):
    (tmp_path / "t").mkdir()
    body = {"watched_folders": [{"path": str(tmp_path / "t")}]}
    hostile = client.put("/settings", json=body,
                         headers={"Origin": "https://evil.example"})
    assert hostile.status_code == 403
    assert client.post("/jobs/sync", headers={"Origin": "null"}).status_code == 403
    assert not config.LOCAL_CONFIG_PATH.exists()


def test_same_origin_and_originless_writes_are_allowed(client):
    own = client.put("/settings", json={}, headers={"Origin": "http://127.0.0.1"})
    assert own.status_code == 200
    assert client.put("/settings", json={}).status_code == 200      # curl-style


def test_reads_are_not_blocked_by_an_origin_header(client):
    assert client.get("/jobs", headers={"Origin": "https://evil.example"}).status_code == 200


# ---------------------------------------------------------------------------
# The CLI is a thin layer over the same service
# ---------------------------------------------------------------------------

def load_cli():
    import importlib.util

    path = config.PROJECT_ROOT / "scripts" / "setup_takeout.py"
    spec = importlib.util.spec_from_file_location("setup_takeout_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_status_and_disable(userdata, fake_chrome, capsys):
    cli = load_cli()
    assert cli.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert "Sync on launch:   True" in out and "Chrome profiles:  all (2 found)" in out

    settings = setup_service.set_drive(localconfig.load(), True)
    localconfig.save(settings)
    assert cli.main(["--disable"]) == 0
    assert localconfig.takeout_enabled(localconfig.load()) is False


def test_cli_add_folder_uses_the_same_validation(userdata, tmp_path, fake_chrome, capsys):
    cli = load_cli()
    assert cli.main(["--add-folder", str(tmp_path / "missing")]) == 1
    assert "not found" in capsys.readouterr().err
    (tmp_path / "t").mkdir()
    assert cli.main(["--add-folder", str(tmp_path / "t"), "--label", "Personal"]) == 0
    assert localconfig.watched_folders(localconfig.load())[0]["label"] == "Personal"
