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
