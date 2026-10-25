"""Setup and Settings logic, shared by the app window and the setup CLI.

The first-run screens, the Settings screen (both via app/api.py), and
scripts/setup_takeout.py all change the same settings in the same ways.
Everything that decides or validates lives here, so the two front ends
are thin: the API turns requests into these calls, and the CLI turns
terminal prompts into them. Neither holds rules of its own.

Validation happens here, on the server side, whatever the front end
already checked: a folder must exist and be readable, a client secret must
parse as an OAuth client. A failed check raises SetupError, whose message
is written for the person using the app (the API returns it as the body of
a 400, the CLI prints it).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import config
from app import drive, folders, localconfig, storage, sync
from extract import extract_history

# A sign-in the user walks away from gives up after this long.
AUTHORIZE_TIMEOUT_S = 300

# The name an imported client secret is saved under in the user-data dir.
CLIENT_SECRET_NAME = "client_secret.json"

# Upper bound for the minimum sync interval: a month. Anything longer would
# let local Chrome history (kept ~90 days) expire between syncs too easily.
MAX_INTERVAL_HOURS = 24 * 30


class SetupError(ValueError):
    """A user-correctable problem, with a message meant for that user."""


# ---------------------------------------------------------------------------
# Reading the current state
# ---------------------------------------------------------------------------

def is_first_run(path: str | Path | None = None) -> bool:
    """No settings file yet: show the first-run screens, skip the auto sync."""
    return not Path(path or config.LOCAL_CONFIG_PATH).exists()


def chrome_profiles(settings: dict, count_visits: bool = True) -> list[dict]:
    """Detected Chrome profiles, each marked selected or not.

    count_visits reads each profile's History database (a temp copy, so it
    works while Chrome is open) to show how many Wikipedia visits it holds.
    That is what Chrome still has (about the last 90 days), not what the
    app has saved over time, so it is normally smaller than the dashboard;
    that is a second or so per profile, which is why the API serves it from
    its own endpoint. An unreadable profile reports None, not an error.
    """
    chosen = {name.lower() for name in localconfig.chrome_profiles(settings)}
    enabled = localconfig.local_enabled(settings)
    out = []
    for profile in extract_history.find_chrome_profiles():
        visits = None
        if count_visits:
            try:
                rows = extract_history.load_chrome_db(Path(profile["path"]))
                visits = extract_history.build_visits(rows)[1]["kept"]
            except Exception:
                visits = None
        selected = enabled and (
            not chosen
            or profile["directory"].lower() in chosen
            or profile["display_name"].lower() in chosen
        )
        out.append({
            "directory": profile["directory"],
            "display_name": profile["display_name"],
            "account": profile["account"],
            "wikipedia_visits": visits,
            "selected": selected,
        })
    return out


def inspect_folder(path: str | Path) -> dict:
    """Check a folder the way a sync would, and describe what it found.

    Used both before adding a folder (the picker's result) and to show the
    current state of each watched folder. Never raises: a missing or
    unreadable folder is a status, with a message saying what to do.
    """
    raw = str(path or "").strip()
    if not raw:
        return {"path": "", "status": "missing", "zips_found": 0,
                "message": "No folder was given."}
    resolved = localconfig.folder_key(raw)
    try:
        scan = folders.scan(resolved)
    except folders.FolderUnavailable as exc:
        return {"path": resolved, "status": exc.status, "zips_found": 0,
                "message": str(exc)}
    count = len(scan.zips)
    if count:
        message = f"{count} Takeout archive{'s' if count != 1 else ''} found."
    else:
        message = ("No Takeout archives found yet. That is fine if your first "
                   "export has not arrived; the app checks again every sync.")
    return {"path": resolved, "status": "ok", "zips_found": count,
            "message": message}


def status(settings: dict, inspect_folders: bool = True) -> dict:
    """Everything the Settings screen shows, in one structure.

    Re-authorization comes from the last sync's state, not from a live
    check: asking Google on every screen open would be slow and is the
    sync's job.
    """
    sync_state = sync.load_state()
    needs_reauth = set(sync_state.get("reauthorize", []))
    watched = []
    for entry in localconfig.watched_folders(settings):
        row = {"path": entry["path"], "label": entry["label"]}
        if inspect_folders:
            found = inspect_folder(entry["path"])
            row.update(status=found["status"], zips_found=found["zips_found"],
                       message=found["message"])
        watched.append(row)

    secret = localconfig.client_secret_path(settings)
    takeout = settings.get("takeout", {})
    return {
        "first_run": is_first_run(),
        "paths": {
            "user_data": str(config.USER_DATA_DIR),
            "settings_file": str(config.LOCAL_CONFIG_PATH),
            "visit_log": str(config.VISIT_LOG_PATH),
            "logs": str(config.LOG_DIR),
        },
        "local": {
            "enabled": localconfig.local_enabled(settings),
            "chrome_profiles": localconfig.chrome_profiles(settings),
        },
        "watched_folders": watched,
        "sync": {
            "on_launch": localconfig.sync_on_launch(settings),
            "min_interval_hours": localconfig.min_sync_interval_hours(settings),
        },
        "takeout": {
            "enabled": localconfig.takeout_enabled(settings),
            "libraries_available": drive.libraries_available(),
            "client_secret": secret.name if secret else None,
            "drive_folder": str(takeout.get("drive_folder_id") or ""),
            "accounts": [
                {
                    "email": email,
                    "token_present": localconfig.token_path(email).exists(),
                    "needs_reauthorization": email in needs_reauth
                    or not localconfig.token_path(email).exists(),
                }
                for email in localconfig.account_emails(settings)
            ],
        },
        "warning": incomplete_warning(settings),
        # The last sync's outcome, so Settings can show what went wrong
        # without the user hunting through the log.
        "last_sync": {
            "attempted": sync_state.get("last_attempt"),
            "succeeded": sync_state.get("last_success"),
            "errors": sync_state.get("errors", []),
            "reauthorize": sorted(needs_reauth),
        },
    }


def incomplete_warning(settings: dict) -> str | None:
    """Say plainly when the Drive route is on but can never sync anything.

    Enabling it without finishing authorization is easy (the sign-in can be
    cancelled or fail), and it produces settings that look complete but
    only warn on every sync.
    """
    if not localconfig.takeout_enabled(settings):
        return None
    if localconfig.account_emails(settings):
        return None
    return ("Google Drive (Advanced) is turned on, but no Google account is "
            "authorized, so it will find nothing. Add an account, or turn "
            "it off.")


# ---------------------------------------------------------------------------
# Changing settings (callers save)
# ---------------------------------------------------------------------------

def set_profiles(settings: dict, selected: list[str] | None) -> dict:
    """Record which Chrome profiles to read.

    selected=None means all of them, stored as the empty list so profiles
    created later are read too (the front ends send None when every box is
    ticked). An empty selection turns the local source off (local.enabled)
    rather than storing an empty list, which would mean "all".
    """
    local = settings.setdefault("local", {})
    names = [str(name).strip() for name in (selected or []) if str(name).strip()]
    if selected is None:
        local["enabled"] = True
        local["chrome_profiles"] = []
    elif not names:
        local["enabled"] = False
        local["chrome_profiles"] = []
    else:
        local["enabled"] = True
        local["chrome_profiles"] = names
    return settings


def add_folder(settings: dict, path: str | Path, label: str = "") -> dict:
    """Add (or relabel) a watched folder, after checking it is usable."""
    found = inspect_folder(path)
    if found["status"] != "ok":
        raise SetupError(found["message"])
    return localconfig.add_folder(settings, found["path"], label)


def set_folders(settings: dict, entries: list[dict]) -> dict:
    """Replace the watched-folder list with the given [{path, label}].

    Only folders that are NEW are required to be reachable right now. A
    folder already in the settings may be on a drive that is unplugged at
    the moment; refusing to save because of it would make the user delete
    and re-add it every time.
    """
    existing = {entry["path"] for entry in localconfig.watched_folders(settings)}
    settings["watched_folders"] = []
    for entry in entries or []:
        path = str((entry or {}).get("path") or "").strip()
        label = str((entry or {}).get("label") or "").strip()
        if not path:
            continue
        if localconfig.folder_key(path) in existing:
            localconfig.add_folder(settings, path, label)
        else:
            add_folder(settings, path, label)
    return settings


def set_sync(settings: dict, on_launch, min_interval_hours) -> dict:
    try:
        hours = float(min_interval_hours)
    except (TypeError, ValueError):
        raise SetupError("The minimum time between syncs must be a number of hours.")
    if not 0 <= hours <= MAX_INTERVAL_HOURS:
        raise SetupError(
            f"The minimum time between syncs must be between 0 and "
            f"{MAX_INTERVAL_HOURS} hours."
        )
    return localconfig.set_sync_preferences(settings, bool(on_launch), hours)


def set_hidden_accounts(settings: dict, hidden) -> dict:
    """Which accounts the dashboard leaves out. Nothing is deleted."""
    if not isinstance(hidden, (list, tuple)):
        raise SetupError("hidden_accounts must be a list of account names.")
    settings.setdefault("dashboard", {})["hidden_accounts"] = list(
        dict.fromkeys(str(account).strip() for account in hidden)
    )
    return settings


def dashboard_accounts(conn, settings: dict) -> list[dict]:
    """The accounts the "Show in dashboard" box lists, each marked shown or
    hidden, from the database (not from the sync sources: an account whose
    folder was removed still has history worth showing or hiding)."""
    hidden = set(localconfig.hidden_accounts(settings))
    return [
        {**entry, "shown": entry["account"] not in hidden}
        for entry in storage.account_summary(conn)
    ]


def set_drive(settings: dict, enabled: bool, drive_folder: str | None = None) -> dict:
    takeout = settings.setdefault("takeout", {})
    takeout["enabled"] = bool(enabled)
    if drive_folder is not None:
        takeout["drive_folder_id"] = str(drive_folder).strip()
    return settings


def apply(settings: dict, changes: dict) -> dict:
    """Apply a whole Settings screen (or the first-run answers) at once.

    Recognized keys, all optional: local {enabled, chrome_profiles},
    watched_folders [{path, label}], sync {on_launch, min_interval_hours},
    takeout {enabled, drive_folder}, dashboard {hidden_accounts}.
    Everything is validated before the caller saves, so a rejected change
    never leaves a half-written file.
    """
    if not isinstance(changes, dict):
        raise SetupError("Settings must be sent as a JSON object.")
    if "local" in changes:
        # {enabled: false} = read no profile; an empty or absent
        # chrome_profiles list = read all of them; otherwise just those.
        local = changes["local"] or {}
        if local.get("enabled", True) is False:
            set_profiles(settings, [])
        else:
            profiles = local.get("chrome_profiles")
            set_profiles(settings, list(profiles) if profiles else None)
    if "watched_folders" in changes:
        set_folders(settings, changes["watched_folders"])
    if "sync" in changes:
        sync_changes = changes["sync"] or {}
        set_sync(
            settings,
            sync_changes.get("on_launch", localconfig.sync_on_launch(settings)),
            sync_changes.get("min_interval_hours",
                             localconfig.min_sync_interval_hours(settings)),
        )
    if "takeout" in changes:
        takeout = changes["takeout"] or {}
        set_drive(settings, takeout.get("enabled", False), takeout.get("drive_folder"))
    if "dashboard" in changes:
        set_hidden_accounts(settings, (changes["dashboard"] or {}).get("hidden_accounts", []))
    return settings


# ---------------------------------------------------------------------------
# Advanced: the user's own OAuth client
# ---------------------------------------------------------------------------

def import_client_secret(settings: dict, path: str | Path) -> dict:
    """Validate a downloaded client-secret JSON and copy it into user data.

    Copied rather than referenced, because a download in ~/Downloads tends
    to be cleaned up, and the secret is a credential that belongs with the
    other user data. Owner-only permissions, written via a temp file so a
    failure never leaves a half-copied secret behind.
    """
    source = Path(str(path or "").strip()).expanduser()
    if not source.is_file():
        raise SetupError(f"File not found: {source}")
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"{source.name} is not a readable JSON file ({exc}).")
    client = None
    if isinstance(data, dict):
        client = data.get("installed") or data.get("web")
    if not isinstance(client, dict) or not client.get("client_id") \
            or not client.get("client_secret"):
        raise SetupError(
            f"{source.name} does not look like an OAuth client secret. In "
            "Google Cloud Console, create a client of type Desktop app and "
            "download its JSON."
        )
    if "installed" not in data:
        raise SetupError(
            f"{source.name} is a Web application client. This app needs a "
            "client of type Desktop app."
        )

    dest = Path(config.USER_DATA_DIR) / CLIENT_SECRET_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, dest)
    finally:
        Path(tmp).unlink(missing_ok=True)
    settings.setdefault("takeout", {})["client_secret"] = CLIENT_SECRET_NAME
    return settings


def authorize(settings: dict, email_hint: str | None = None,
              timeout_seconds: float = AUTHORIZE_TIMEOUT_S) -> str:
    """Sign a Google account in through the browser; returns its email.

    Adding an account switches the Drive route on: authorizing an account
    is an unambiguous request to use it.
    """
    if not drive.libraries_available():
        raise SetupError("The Google libraries are not installed. "
                         + drive.INSTALL_HINT)
    if localconfig.client_secret_path(settings) is None:
        raise SetupError("Choose your OAuth client secret file first.")
    try:
        email = drive.authorize_account(
            settings, email_hint=email_hint, timeout_seconds=timeout_seconds
        )
    except drive.DriveError as exc:
        raise SetupError(str(exc)) from exc
    record_authorized(settings, email)
    return email


def record_authorized(settings: dict, email: str) -> dict:
    """Add a freshly authorized account and switch the Drive route on.

    Separate from authorize() so a caller that waited minutes for the
    browser can apply the result to a FRESHLY loaded copy of the settings,
    instead of saving the copy it loaded before the wait over any change
    made in the meantime.
    """
    localconfig.add_account(settings, email)
    settings.setdefault("takeout", {})["enabled"] = True
    return settings


def remove_account(settings: dict, email: str) -> dict:
    """Forget an account: remove it from the settings and delete its token.

    The token is a live credential for that account's Drive; keeping it on
    disk after the user asked to remove the account would be surprising.
    """
    localconfig.remove_account(settings, email)
    localconfig.token_path(email).unlink(missing_ok=True)
    return settings
