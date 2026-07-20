"""Central configuration: paths, API etiquette, timezone, request policy.

Every module reads settings from here; nothing else hardcodes paths or URLs.
"""

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
# External APIs (repo_spec S8) — Wikimedia requires a real contact address
# ---------------------------------------------------------------------------
USER_AGENT = "WikipediaWrapped/0.1 (emerjoe1779@gmail.com)"

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

# ---------------------------------------------------------------------------
# Timezone (stats are computed in local time; repo_spec §7.3)
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
