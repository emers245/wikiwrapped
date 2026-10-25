"""Per-user preferences: config.local.toml in the user-data dir (D10).

Separate from config.py on purpose. config.py holds decisions that are the
same for everyone and belong in git; this holds one user's choices and
credentials-adjacent settings, and lives with the rest of the user data
(config.USER_DATA_DIR), never in the program folder.

TOML because tomllib is stdlib on the pinned Python 3.11 (no new dependency)
and because comments survive, so the file can document its own options.
tomllib is read-only, so writing goes through a small emitter below.

The emitter walks whatever dict it is handed rather than knowing the schema
field by field. That is what makes the setup flow safely re-runnable: a key
this version does not understand still round-trips through a load/save
instead of being silently dropped.

Shape:

    [sync]
    on_launch = true             # one pass over every source at launch
    min_interval_hours = 24

    [[watched_folders]]          # Takeout zips on this computer (no sign-in)
    path = "/Users/you/Google Drive/Takeout"
    label = "Personal"

    [takeout]                    # Advanced: Drive via your own OAuth client
    enabled = true

    [[takeout.accounts]]
    email = "you@example.com"

Nothing here is required. With the file absent, load() returns defaults:
sync on launch, no watched folders, the Drive integration off, and the
local-Chrome path reading every profile.

The three input sources are independent. Local Chrome is read unless
local.enabled is false;
watched folders are read whenever any are configured; the Drive path runs
only when takeout.enabled is true. Either Takeout route, both, or neither
can be in use.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import config

DEFAULTS: dict = {
    # Sync preferences cover every source, so they live in their own table
    # rather than under [takeout], where they used to be. On by default:
    # Chrome expires local history after about 90 days, and a launch is the
    # only moment the app gets to capture it (D9, D12).
    "sync": {
        "on_launch": True,
        "min_interval_hours": config.MIN_SYNC_INTERVAL_HOURS,
    },
    # Folders scanned for Takeout zips, as [{path, label}]. The label stands
    # in for the account, because a zip on disk does not say whose it is.
    "watched_folders": [],
    # The Drive/OAuth route is opt-in and stays off unless set up.
    "takeout": {
        "enabled": False,
        # Empty means "discover it": the client secret by glob in the
        # user-data dir, the archives by name anywhere in Drive.
        "client_secret": "",
        "drive_folder_id": "",
        "accounts": [],
    },
    "local": {
        # Off only when someone unticks every profile in setup: an empty
        # chrome_profiles list already means "all of them", so "none" needs
        # its own switch rather than an empty list.
        "enabled": True,
        # Which Chrome profiles to read. Empty means every profile found,
        # because the signed-in profile is often not "Default" and picking
        # one by default would silently capture nothing. Entries match the
        # directory name ("Profile 1") or the display name ("Work").
        "chrome_profiles": [],
    },
}


def local_enabled(settings: dict) -> bool:
    """Whether the local Chrome history is read at all."""
    return bool(settings.get("local", {}).get("enabled", True))


def chrome_profiles(settings: dict) -> list[str]:
    """Configured profile filter; empty list means read all of them."""
    raw = settings.get("local", {}).get("chrome_profiles", [])
    if isinstance(raw, str):            # tolerate a hand-edited bare string
        raw = [raw]
    return [str(name).strip() for name in raw if str(name).strip()]


def load(path: str | Path | None = None) -> dict:
    """Read config.local.toml, filling in defaults for anything absent."""
    path = Path(path or config.LOCAL_CONFIG_PATH)
    merged = copy.deepcopy(DEFAULTS)
    if not path.exists():
        return merged
    with open(path, "rb") as handle:
        loaded = tomllib.load(handle)
    _migrate_legacy_sync_keys(loaded)
    return _deep_merge(merged, loaded)


# Where the sync preferences lived before they got their own table.
_LEGACY_SYNC_KEYS = {
    "sync_on_launch": "on_launch",
    "min_sync_interval_hours": "min_interval_hours",
}


def _migrate_legacy_sync_keys(loaded: dict) -> None:
    """Move takeout.sync_on_launch / min_sync_interval_hours into [sync].

    In memory only; the file changes the next time settings are saved, at
    which point the old keys are gone and [sync] is the single source of
    truth. An explicit [sync] value always wins over a legacy one.

    The legacy on-launch flag is carried over only when the Drive route was
    enabled. It used to gate the whole launch sync on Takeout being on, and
    disabling Takeout also switched it off, so a false there usually meant
    "Takeout is off", not "do not capture local Chrome history". Treating it
    as the latter would keep the 90-day local history from ever syncing.
    """
    takeout = loaded.get("takeout")
    if not isinstance(takeout, dict):
        return
    target = loaded.setdefault("sync", {})
    if not isinstance(target, dict):
        return
    for old, new in _LEGACY_SYNC_KEYS.items():
        if old not in takeout:
            continue
        value = takeout.pop(old)
        if new in target:
            continue
        if old == "sync_on_launch" and not takeout.get("enabled"):
            continue
        target[new] = value


def save(settings: dict, path: str | Path | None = None) -> Path:
    """Write settings back as TOML. Overwrites the whole file."""
    path = Path(path or config.LOCAL_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Local settings for Wikipedia Wrapped (per-user, never in git).\n"
        "# Written by the app's setup; safe to edit by hand.\n\n"
    )
    path.write_text(header + _emit(settings), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Typed accessors: the rest of the app asks these, never the raw dict
# ---------------------------------------------------------------------------

def takeout_enabled(settings: dict) -> bool:
    return bool(settings.get("takeout", {}).get("enabled"))


def sync_on_launch(settings: dict) -> bool:
    """Whether launching the app runs a sync pass over every source.

    Independent of which sources are enabled: local Chrome is always a
    source, and it is the one whose history expires.
    """
    return bool(settings.get("sync", {}).get("on_launch", True))


def min_sync_interval_hours(settings: dict) -> float:
    value = settings.get("sync", {}).get(
        "min_interval_hours", config.MIN_SYNC_INTERVAL_HOURS
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(config.MIN_SYNC_INTERVAL_HOURS)


def set_sync_preferences(
    settings: dict, on_launch: bool, min_interval_hours: float | None = None
) -> dict:
    sync = settings.setdefault("sync", {})
    sync["on_launch"] = bool(on_launch)
    if min_interval_hours is not None:
        sync["min_interval_hours"] = max(0.0, float(min_interval_hours))
    return settings


# ---------------------------------------------------------------------------
# Watched folders: Takeout zips on this computer
# ---------------------------------------------------------------------------

def folder_key(path: str | Path) -> str:
    """Canonical form of a folder path, so one folder is never listed twice.

    resolve() follows symlinks and normalizes "..", and does not require the
    folder to exist (a disconnected drive must still dedupe correctly).
    """
    return str(Path(path).expanduser().resolve())


def watched_folders(settings: dict) -> list[dict]:
    """Configured folders as [{path, label}], deduped, in config order.

    Tolerates a hand-edited bare string in place of a {path, label} table.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for entry in settings.get("watched_folders", []) or []:
        if isinstance(entry, str):
            entry = {"path": entry}
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("path") or "").strip()
        if not raw:
            continue
        key = folder_key(raw)
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": key, "label": str(entry.get("label") or "").strip()})
    return out


def add_folder(settings: dict, path: str | Path, label: str = "") -> dict:
    """Add a watched folder, or update the label of one already listed."""
    raw = str(path).strip()
    if not raw:
        raise ValueError("Folder path must not be empty.")
    key = folder_key(raw)
    label = (label or "").strip()
    folders = settings.setdefault("watched_folders", [])
    for entry in folders:
        if isinstance(entry, dict) and folder_key(entry.get("path") or "") == key:
            entry["path"] = key
            entry["label"] = label
            return settings
    folders.append({"path": key, "label": label})
    return settings


def remove_folder(settings: dict, path: str | Path) -> dict:
    key = folder_key(path)
    settings["watched_folders"] = [
        entry
        for entry in settings.get("watched_folders", []) or []
        if not (
            (isinstance(entry, dict) and folder_key(entry.get("path") or "") == key)
            or (isinstance(entry, str) and folder_key(entry) == key)
        )
    ]
    return settings


def account_emails(settings: dict) -> list[str]:
    """Authorized account addresses, in config order, deduped."""
    out = []
    for entry in settings.get("takeout", {}).get("accounts", []):
        email = (entry or {}).get("email", "").strip() if isinstance(entry, dict) else ""
        if email and email not in out:
            out.append(email)
    return out


def add_account(settings: dict, email: str) -> dict:
    """Add an account if new; returns the same dict for chaining."""
    email = email.strip()
    if not email:
        raise ValueError("Account email must not be empty.")
    accounts = settings.setdefault("takeout", {}).setdefault("accounts", [])
    if not any(
        isinstance(a, dict) and a.get("email", "").strip() == email for a in accounts
    ):
        accounts.append({"email": email})
    return settings


def remove_account(settings: dict, email: str) -> dict:
    email = email.strip()
    takeout = settings.setdefault("takeout", {})
    takeout["accounts"] = [
        a
        for a in takeout.get("accounts", [])
        if not (isinstance(a, dict) and a.get("email", "").strip() == email)
    ]
    return settings


def token_path(email: str) -> Path:
    """Where this account's OAuth token cache lives (one file per account)."""
    return Path(config.TOKEN_DIR) / f"{_safe_filename(email)}.json"


def client_secret_path(settings: dict) -> Path | None:
    """Configured client secret, else the first glob match in the user-data dir.

    A relative setting is resolved against the user-data dir too, not the
    program folder: the secret is a credential, so it is user data.

    Returns None when no secret is present, which callers report as "run
    setup first" rather than crashing.
    """
    configured = settings.get("takeout", {}).get("client_secret", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not candidate.is_absolute():
            candidate = Path(config.USER_DATA_DIR) / candidate
        return candidate if candidate.exists() else None
    matches = sorted(Path(config.USER_DATA_DIR).glob(config.CLIENT_SECRET_GLOB))
    return matches[0] if matches else None


def drive_folder_id(settings: dict) -> str | None:
    value = settings.get("takeout", {}).get("drive_folder_id", "").strip()
    return value or None


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _safe_filename(email: str) -> str:
    """Make an email safe as a filename without losing which account it is."""
    keep = [c if (c.isalnum() or c in "@.-_") else "_" for c in email.strip()]
    return "".join(keep) or "account"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Overlay wins, except that nested dicts merge key by key."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _emit(settings: dict, prefix: str = "") -> str:
    """Serialize a dict of scalars/lists/dicts to TOML.

    TOML requires a table's own scalars before any of its sub-tables, so the
    three kinds are emitted in passes rather than in dict order.
    """
    scalars, tables, table_arrays = [], [], []
    for key, value in settings.items():
        if isinstance(value, dict):
            tables.append((key, value))
        elif isinstance(value, list) and value and all(
            isinstance(item, dict) for item in value
        ):
            table_arrays.append((key, value))
        else:
            scalars.append((key, value))

    lines = [f"{_key(k)} = {_value(v)}" for k, v in scalars]

    for key, value in tables:
        name = f"{prefix}{_key(key)}"
        lines.append("")
        lines.append(f"[{name}]")
        body = _emit(value, prefix=f"{name}.")
        if body:
            lines.append(body.rstrip("\n"))

    for key, entries in table_arrays:
        name = f"{prefix}{_key(key)}"
        for entry in entries:
            lines.append("")
            lines.append(f"[[{name}]]")
            body = _emit(entry, prefix=f"{name}.")
            if body:
                lines.append(body.rstrip("\n"))

    return "\n".join(lines) + "\n" if lines else ""


def _key(key: str) -> str:
    """Bare key where TOML allows it, quoted otherwise."""
    text = str(key)
    ok = text and all(c.isalnum() or c in "-_" for c in text)
    return text if ok else _quote(text)


def _value(value) -> str:
    if isinstance(value, bool):     # before int: bool is a subclass of int
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return _quote(str(value))


def _quote(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'
