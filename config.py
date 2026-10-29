"""Central configuration: paths, API etiquette, timezone, request policy.

Every module reads settings from here; nothing else hardcodes paths or URLs.

Path constants are resolved once, at import, from environment variables and
the platform. Resolving them creates nothing: directories are made by the
code that writes into them, so importing config has no side effects.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

APP_NAME = "WikiWrapped"
# The one place the version is set: the installers and the app's conda
# package take it from here (packaging/build_installer.py), and a release's
# git tag must be v<this>.
APP_VERSION = "0.1.0"
PROJECT_URL = "https://github.com/emers245/wikiwrapped"

# ---------------------------------------------------------------------------
# Paths: two kinds of storage, kept strictly apart
# ---------------------------------------------------------------------------
# The program folder is read-only at runtime. In an installed app it may sit
# somewhere the user cannot write (and on macOS, writing into a signed app
# bundle breaks its signature), so nothing the app creates or changes may
# live there. Everything is one of:
#
# - USER DATA (writable, per user): the database, the visit log, sync state,
#   settings, OAuth credentials, logs, caches. Lives in the platform's
#   standard per-user location, or wherever WIKIPEDIA_WRAPPED_DATA_DIR
#   points (the dev override, for working against a scratch directory).
#
# - APP RESOURCES (read-only, shipped with the program): the frozen UMAP
#   reference space, and later the bundled embedding model.
PROJECT_ROOT = Path(__file__).resolve().parent

USER_DATA_ENV_VAR = "WIKIPEDIA_WRAPPED_DATA_DIR"
RESOURCE_ENV_VAR = "WIKIPEDIA_WRAPPED_RESOURCE_DIR"


def user_data_dir() -> Path:
    """Root of this user's writable data.

    The override wins so a developer (or the test suite) can point the whole
    app at a scratch directory without touching real data. Otherwise the
    platform default, which platformdirs knows for every OS:

        macOS    ~/Library/Application Support/WikiWrapped
        Windows  %APPDATA%\\WikiWrapped   (roaming=True)
        Linux    $XDG_DATA_HOME/WikiWrapped, else ~/.local/share/...

    appauthor=False stops Windows from nesting it under a company folder.
    """
    override = os.environ.get(USER_DATA_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    import platformdirs

    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False, roaming=True))


# The folder, next to the code, where an installer puts the bundled
# resources (the model and the reference space). Git-ignored in a checkout.
INSTALLED_RESOURCES_NAME = "resources"


def installed_resource_dir() -> Path | None:
    """Where an installed copy of the app keeps its read-only resources.

    The installers put the app's code in one folder and the bundled
    resources in a "resources" folder beside it (packaging/README.md), so
    the resources are found relative to this file and nothing about the
    install location needs configuring. A dev checkout has no such folder
    (it is git-ignored), so this returns None there and resolution falls
    through to the repo's data/.
    """
    candidate = PROJECT_ROOT / INSTALLED_RESOURCES_NAME
    return candidate if candidate.is_dir() else None


def is_installed() -> bool:
    """True when running from an installer's layout (bundled resources).

    Installed mode is stricter than a dev checkout: the model must load from
    the bundled copy and nothing may be fetched from the internet to fill a
    gap (app/embeddings.py), because a missing file means a broken install,
    which should fail loudly rather than quietly download.
    """
    return installed_resource_dir() is not None


def resource_dir() -> Path:
    """Root of the read-only app resources, in priority order:

    1. WIKIPEDIA_WRAPPED_RESOURCE_DIR, for testing a resource set by hand.
    2. installed_resource_dir(), next to an installed app.
    3. The repo's data/ directory, for a dev checkout.
    """
    override = os.environ.get(RESOURCE_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    installed = installed_resource_dir()
    if installed is not None and installed.exists():
        return installed
    return PROJECT_ROOT / "data"


USER_DATA_DIR = user_data_dir()

# The layout under USER_DATA_DIR deliberately mirrors the old repo layout
# (data/, tokens/, config.local.toml at the top), so app.migrate copies each
# legacy file to the same relative path and the mapping is easy to check.
DATA_DIR = USER_DATA_DIR / "data"
DB_PATH = DATA_DIR / "app.db"

# Append-only log of raw visits: the durable source of truth (D9). app.db is a
# rebuildable derived artifact; this file is the thing worth backing up.
VISIT_LOG_PATH = DATA_DIR / "visits.jsonl"

# Last-sync bookkeeping for the check-on-launch pass, so a relaunch shortly
# after a sync can no-op without touching the network.
SYNC_STATE_PATH = DATA_DIR / "sync_state.json"

LOG_DIR = USER_DATA_DIR / "logs"
CACHE_DIR = USER_DATA_DIR / "cache"

# Frozen UMAP reducer + reference points (D4): a read-only resource.
REFERENCE_DIR = resource_dir() / "reference"

# The bundled sentence-transformers model (see EMBEDDING_MODEL below): a
# read-only resource, fetched by scripts/fetch_model.py.
MODELS_DIR = resource_dir() / "models"

# Where scripts/build_reference_space.py writes a fresh reference space. A
# developer tool, so it is the one thing allowed to write into the repo; the
# app itself only ever reads REFERENCE_DIR.
DEV_REFERENCE_DIR = PROJECT_ROOT / "data" / "reference"

# The pre-migration locations, read only by app.migrate (which copies them,
# never moves them) and by the "not migrated yet" warning.
LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_CONFIG_PATH = PROJECT_ROOT / "config.local.toml"
LEGACY_TOKEN_DIR = PROJECT_ROOT / "tokens"

# ---------------------------------------------------------------------------
# Google Drive / Takeout integration (D10)
# ---------------------------------------------------------------------------
# Everything here is credentials or personal preference, so it is user data,
# and none of it is required for the local-Chrome path to work.
LOCAL_CONFIG_PATH = USER_DATA_DIR / "config.local.toml"
TOKEN_DIR = USER_DATA_DIR / "tokens"         # one OAuth token per account
CLIENT_SECRET_GLOB = "client_secret*.json"   # downloaded from Cloud Console

# Read-only, and deliberately the narrowest scope that can list and download
# a Takeout archive. The app never asks for, receives, or stores a password;
# the OAuth token is the only credential it ever holds.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Takeout names its archives "takeout-YYYYMMDDTHHMMSSZ-<nnn>.zip". Matched
# case-insensitively by prefix so a renamed-but-recognizable file still lands.
TAKEOUT_NAME_PATTERN = "takeout"

# Takeout delivers a new archive every 2 months at most, so a launch inside
# this window skips the Drive round-trip entirely.
MIN_SYNC_INTERVAL_HOURS = 24

# Drive media downloads (files?alt=media) are noticeably less reliable than
# the metadata API on some networks: the connection is accepted and then
# never sends a byte, while metadata calls to the same host answer in
# milliseconds. Measured on this machine 2026-08-09, a 54 KB archive failed
# 3/3 times without an explicit gzip Accept-Encoding and 1/3 times with it.
#
# So: ask for gzip, keep the read timeout short enough that a dead
# connection is abandoned quickly rather than burning a minute, and retry.
# A slow-but-working link is not punished, because the timeout is per read,
# not for the whole transfer.
DRIVE_CONNECT_TIMEOUT_S = 10
DRIVE_READ_TIMEOUT_S = 20
DRIVE_DOWNLOAD_ATTEMPTS = 5
DRIVE_DOWNLOAD_BACKOFF_S = 2.0

# ---------------------------------------------------------------------------
# Input schema (repo_spec S5)
# ---------------------------------------------------------------------------
SCHEMA_VERSION = "1.0"

# ---------------------------------------------------------------------------
# External APIs (repo_spec S8)
# ---------------------------------------------------------------------------
# Wikimedia's User-Agent policy asks for a way to contact the operator of a
# tool, and accepts a URL as well as an email. The project URL identifies
# the app for every user, so nobody has to hand over a personal address.
# A developer can still append their own with WIKIPEDIA_WRAPPED_EMAIL.
CONTACT_EMAIL_ENV_VAR = "WIKIPEDIA_WRAPPED_EMAIL"


def get_user_agent() -> str:
    """WikiWrapped/<version> (<project url>[; <optional email>])."""
    contact = PROJECT_URL
    email = os.environ.get(CONTACT_EMAIL_ENV_VAR, "").strip()
    if email:
        contact = f"{contact}; {email}"
    return f"{APP_NAME}/{APP_VERSION} ({contact})"


WIKI_LANG = "en"
MEDIAWIKI_API_URL = f"https://{WIKI_LANG}.wikipedia.org/w/api.php"
SUMMARY_API_URL = f"https://{WIKI_LANG}.wikipedia.org/api/rest_v1/page/summary/"
LIFTWING_TOPIC_URL = (
    "https://api.wikimedia.org/service/lw/inference/v1/models/"
    "outlink-topic-model:predict"
)
PAGEVIEWS_API_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    f"{WIKI_LANG}.wikipedia/all-access/user"
)

# Request policy shared by all API clients
REQUEST_TIMEOUT_S = 30
THROTTLE_S = 0.1          # polite delay between requests
MAX_RETRIES = 4
BACKOFF_BASE_S = 2.0      # exponential backoff on 429/5xx: 2s, 4s, 8s, ...

# ---------------------------------------------------------------------------
# Embeddings / UMAP
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# The exact Hugging Face commit of that model, pinned so every install embeds
# with byte-identical weights: the reference space was built from vectors of
# this revision, and a different one would put new pages in slightly wrong
# places. 1110a243 is the revision the current reference space was built
# with (2026-07-19) and is still the repo's main as of 2026-10-25.
EMBEDDING_MODEL_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"

# Where the bundled copy of the model lives under MODELS_DIR.
EMBEDDING_MODEL_DIR = MODELS_DIR / EMBEDDING_MODEL.rsplit("/", 1)[-1]

# Reference-space sample sizes (D4): top-viewed head + random tail.
# Decision 2026-07-19: ~40k total matches the old sampler's proven scale.
REFERENCE_HEAD_SIZE = 30_000
REFERENCE_TAIL_SIZE = 10_000

# ---------------------------------------------------------------------------
# Timezone (stats are computed in local time; repo_spec S7.3)
# ---------------------------------------------------------------------------
# None -> auto-detect the device's IANA zone via tzlocal.
# Set to an IANA name (e.g. "America/Chicago") to force a zone.
TIMEZONE_OVERRIDE: str | None = None


def get_timezone(name: str | None = None) -> ZoneInfo:
    """Resolve the timezone for stats: explicit arg > config override > device."""
    if name:
        return ZoneInfo(name)
    if TIMEZONE_OVERRIDE:
        return ZoneInfo(TIMEZONE_OVERRIDE)
    from tzlocal import get_localzone_name

    return ZoneInfo(get_localzone_name())
