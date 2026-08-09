"""Central configuration: paths, API etiquette, timezone, request policy.

Every module reads settings from here; nothing else hardcodes paths or URLs.
"""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"  # frozen UMAP reducer + reference embeddings (D4)
DB_PATH = DATA_DIR / "app.db"

# Append-only log of raw visits: the durable source of truth (D9). app.db is a
# rebuildable derived artifact; this file is the thing worth backing up.
# Under data/, which .gitignore excludes wholesale.
VISIT_LOG_PATH = DATA_DIR / "visits.jsonl"

# Last-sync bookkeeping for the check-on-launch pass, so a relaunch shortly
# after a sync can no-op without touching the network.
SYNC_STATE_PATH = DATA_DIR / "sync_state.json"

# ---------------------------------------------------------------------------
# Google Drive / Takeout integration (D10)
# ---------------------------------------------------------------------------
# Everything here is credentials or personal preference: all four paths are
# git-ignored, and none of them is required for the local-Chrome path to work.
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config.local.toml"
TOKEN_DIR = PROJECT_ROOT / "tokens"          # one OAuth token per account
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
# External APIs (repo_spec S8) - Wikimedia requires a real contact address.
# The email is personal data and is never committed: it is read from the
# WIKIPEDIA_WRAPPED_EMAIL environment variable, falling back to a git-ignored
# contact_email.txt at the repo root.
# ---------------------------------------------------------------------------
CONTACT_EMAIL_ENV_VAR = "WIKIPEDIA_WRAPPED_EMAIL"
CONTACT_EMAIL_FILE = PROJECT_ROOT / "contact_email.txt"


def get_contact_email() -> str:
    email = os.environ.get(CONTACT_EMAIL_ENV_VAR, "").strip()
    if not email and CONTACT_EMAIL_FILE.exists():
        email = CONTACT_EMAIL_FILE.read_text(encoding="utf-8").strip()
    if not email:
        raise RuntimeError(
            "No contact email configured. Wikimedia requires a real contact "
            f"address in API User-Agent headers; set the {CONTACT_EMAIL_ENV_VAR} "
            f"environment variable or create {CONTACT_EMAIL_FILE.name} "
            "(git-ignored) in the project root containing your email."
        )
    return email


def get_user_agent() -> str:
    return f"WikipediaWrapped/0.1 ({get_contact_email()})"


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
