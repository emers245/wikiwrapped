"""Git-ignored local preferences: config.local.toml (D10).

Separate from config.py on purpose. config.py holds decisions that are the
same for everyone and belong in git; this holds one user's choices and
credentials-adjacent settings, and is never committed.

TOML because tomllib is stdlib on the pinned Python 3.11 (no new dependency)
and because comments survive, so the file can document its own options.
tomllib is read-only, so writing goes through a small emitter below.

The emitter walks whatever dict it is handed rather than knowing the schema
field by field. That is what makes the setup flow safely re-runnable: a key
this version does not understand still round-trips through a load/save
instead of being silently dropped.

Shape:

    [takeout]
    enabled = true
    sync_on_launch = true
    min_sync_interval_hours = 24

    [[takeout.accounts]]
    email = "you@example.com"

Nothing here is required. With the file absent, load() returns defaults with
Takeout disabled and the local-Chrome path works exactly as before.
"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import config

# Takeout is opt-in: an untouched install must behave like it did before.
DEFAULTS: dict = {
    "takeout": {
        "enabled": False,
        "sync_on_launch": False,
        "min_sync_interval_hours": config.MIN_SYNC_INTERVAL_HOURS,
        # Empty means "discover it": the client secret by glob in the project
        # root, the archives by name anywhere in Drive.
        "client_secret": "",
        "drive_folder_id": "",
        "accounts": [],
    },
    "local": {
        # Which Chrome profiles to read. Empty means every profile found,
        # because the signed-in profile is often not "Default" and picking
        # one by default would silently capture nothing. Entries match the
        # directory name ("Profile 1") or the display name ("Work").
        "chrome_profiles": [],
    },
}


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
    return _deep_merge(merged, loaded)


def save(settings: dict, path: str | Path | None = None) -> Path:
    """Write settings back as TOML. Overwrites the whole file."""
    path = Path(path or config.LOCAL_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Local settings for wikipedia-wrapped (git-ignored).\n"
        "# Written by scripts/setup_takeout.py; safe to edit by hand.\n\n"
    )
    path.write_text(header + _emit(settings), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Typed accessors: the rest of the app asks these, never the raw dict
# ---------------------------------------------------------------------------

def takeout_enabled(settings: dict) -> bool:
    return bool(settings.get("takeout", {}).get("enabled"))


def sync_on_launch(settings: dict) -> bool:
    """Both flags must be on; enabling Takeout does not imply auto-sync."""
    takeout = settings.get("takeout", {})
    return bool(takeout.get("enabled")) and bool(takeout.get("sync_on_launch"))


def min_sync_interval_hours(settings: dict) -> float:
    value = settings.get("takeout", {}).get(
        "min_sync_interval_hours", config.MIN_SYNC_INTERVAL_HOURS
    )
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(config.MIN_SYNC_INTERVAL_HOURS)


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
    """Configured client secret, else the first match of the root glob.

    Returns None when no secret is present, which callers report as "run
    setup first" rather than crashing.
    """
    configured = settings.get("takeout", {}).get("client_secret", "").strip()
    if configured:
        candidate = Path(configured)
        if not candidate.is_absolute():
            candidate = Path(config.PROJECT_ROOT) / candidate
        return candidate if candidate.exists() else None
    matches = sorted(Path(config.PROJECT_ROOT).glob(config.CLIENT_SECRET_GLOB))
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
