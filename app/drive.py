"""Read-only Google Drive access for Takeout archives (D10).

Why Takeout at all: Chrome expires local history after about 90 days and the
local SQLite DB only ever covers one account on one device. Takeout exports
at the ACCOUNT level (Chrome Sync has already merged every device), reaches
further back, and can be scheduled to drop a fresh archive into Drive every
two months. This is an input source alongside the local Chrome reader, never
a replacement for it.

Boundaries this module holds to:

- Read-only. The single scope is drive.readonly, and nothing here calls a
  create, update, copy, trash, or delete method. Retention of the archives
  is the user's decision, made by hand in Drive.
- No passwords, ever. The app never asks for, receives, stores, or transmits
  a Google password. The only credential it holds is an OAuth token, cached
  per account under tokens/ in the user-data dir.
- No parsing. Archives are handed to extract/extract_history.py, which owns
  every browser-specific format (repo_spec D1). This module is pure I/O.

The google-* libraries are imported lazily inside functions, not at module
scope, so importing app.sync (or anything else) still works on an install
that never enabled Takeout. That is what lets the local-Chrome path keep
running with the integration switched off.
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path

import config
from app import localconfig

log = logging.getLogger(__name__)

# Drive returns pages of results; Takeout folders are small, but a user with
# many archives should still enumerate correctly.
PAGE_SIZE = 100

INSTALL_HINT = (
    "Google Drive support needs three extra packages:\n"
    "    conda env update -f environment.yml\n"
    "or: pip install google-api-python-client google-auth-oauthlib google-auth"
)


class DriveError(RuntimeError):
    """Any Drive problem the sync layer should report but survive."""


class ReauthorizationNeeded(DriveError):
    """A cached token could not be refreshed.

    Expected roughly weekly while the OAuth client is in "Testing" publishing
    status, because Google expires refresh tokens for unverified apps after
    about 7 days. Carries the account so the sync report can name it instead
    of failing anonymously.
    """

    def __init__(self, account: str, detail: str = ""):
        self.account = account
        message = (
            f"Re-authorization needed for account {account}. Run:\n"
            f"    python scripts/setup_takeout.py --authorize {account}"
        )
        if detail:
            message += f"\n(reason: {detail})"
        super().__init__(message)


def _google_modules():
    """Import the Google libraries, or explain how to install them."""
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as exc:
        raise DriveError(f"{INSTALL_HINT}\n(import failed: {exc})") from exc
    return {
        "RefreshError": RefreshError,
        "Request": Request,
        "Credentials": Credentials,
        "InstalledAppFlow": InstalledAppFlow,
        "build": build,
        "HttpError": HttpError,
        "MediaIoBaseDownload": MediaIoBaseDownload,
    }


def libraries_available() -> bool:
    """True when the Google libraries can be imported (for setup/health)."""
    try:
        _google_modules()
    except DriveError:
        return False
    return True


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_credentials(email: str, settings: dict | None = None):
    """Load and, if needed, refresh this account's cached token.

    Never interactive: a missing or unrefreshable token raises
    ReauthorizationNeeded so an unattended sync reports it and moves on
    rather than blocking on a browser prompt.
    """
    google = _google_modules()
    token_file = localconfig.token_path(email)
    if not token_file.exists():
        raise ReauthorizationNeeded(email, f"no cached token at {token_file}")

    try:
        creds = google["Credentials"].from_authorized_user_file(
            str(token_file), config.DRIVE_SCOPES
        )
    except (ValueError, KeyError) as exc:
        raise ReauthorizationNeeded(email, f"token file unreadable: {exc}") from exc

    if creds.valid:
        return creds
    if not (creds.expired and creds.refresh_token):
        raise ReauthorizationNeeded(email, "cached token has no usable refresh token")

    try:
        creds.refresh(google["Request"]())
    except google["RefreshError"] as exc:
        # The common case: the OAuth app is still in Testing, so Google
        # expired the refresh token after about a week.
        raise ReauthorizationNeeded(email, str(exc)) from exc
    except Exception as exc:  # network down, clock skew, transient 5xx
        raise DriveError(f"Could not refresh the token for {email}: {exc}") from exc

    _write_token(token_file, creds)
    return creds


def authorize_account(settings: dict, email_hint: str | None = None) -> str:
    """Run the interactive installed-app OAuth flow; returns the account email.

    Opens a local-server callback (the Desktop app client type). The email is
    read back from Drive rather than trusted from user input, so the token
    filename always matches the account that actually authorized.
    """
    google = _google_modules()
    secret = localconfig.client_secret_path(settings)
    if secret is None:
        raise DriveError(
            "No OAuth client secret found. Download the Desktop app client "
            f"JSON from Google Cloud Console into {config.USER_DATA_DIR} "
            f"(the filename must match {config.CLIENT_SECRET_GLOB}), "
            "or set takeout.client_secret in config.local.toml. "
            "The README has the console steps."
        )

    flow = google["InstalledAppFlow"].from_client_secrets_file(
        str(secret), config.DRIVE_SCOPES
    )
    # port=0 lets the OS pick a free port for the loopback redirect.
    creds = flow.run_local_server(
        port=0,
        prompt="consent",          # force a refresh token to be issued
        access_type="offline",
        authorization_prompt_message=(
            "Opening a browser to authorize read-only Drive access"
            + (f" for {email_hint}" if email_hint else "")
            + ".\nIf it does not open, visit:\n    {url}"
        ),
        success_message=(
            "Authorized. You can close this tab and return to the terminal."
        ),
    )

    email = _account_email(creds) or (email_hint or "").strip()
    if not email:
        raise DriveError(
            "Authorization succeeded but Drive did not report an account "
            "address, so the token cannot be filed per account."
        )
    _write_token(localconfig.token_path(email), creds)
    return email


def _account_email(creds) -> str | None:
    """Which account authorized: about.get is allowed under drive.readonly."""
    google = _google_modules()
    try:
        service = google["build"]("drive", "v3", credentials=creds,
                                  cache_discovery=False)
        about = service.about().get(fields="user(emailAddress)").execute()
        return (about.get("user") or {}).get("emailAddress")
    except Exception as exc:
        log.warning("Could not read the authorized account address: %s", exc)
        return None


def _write_token(token_file: Path, creds) -> None:
    """Cache a token with owner-only permissions."""
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json(), encoding="utf-8")
    try:
        token_file.chmod(0o600)
    except OSError:  # filesystems without POSIX modes
        pass


# ---------------------------------------------------------------------------
# Discovery + download (read-only)
# ---------------------------------------------------------------------------

def _service(creds):
    google = _google_modules()
    return google["build"]("drive", "v3", credentials=creds, cache_discovery=False)


def resolve_folder_id(creds, value: str | None) -> str | None:
    """Accept either a Drive folder ID or a folder NAME, return an ID.

    Setup asks for a folder and people reasonably type "Takeout", which is
    the name they see in Drive, not the opaque ID from the URL bar. Querying
    "'Takeout' in parents" matches nothing and reports zero archives with no
    error, so the friendly input has to be resolved rather than rejected.

    Tries the value as an ID first (one cheap metadata call), then falls back
    to a name search. Returns None if neither resolves, which the caller
    treats as "search the whole Drive" rather than as a failure.
    """
    if not value:
        return None
    google = _google_modules()
    service = _service(creds)

    try:
        found = service.files().get(
            fileId=value, fields="id, mimeType"
        ).execute()
        if found.get("mimeType") == "application/vnd.google-apps.folder":
            return found["id"]
        log.warning("Drive item %s is not a folder; ignoring the setting.", value)
        return None
    except google["HttpError"]:
        pass  # not an ID; try it as a name
    except Exception as exc:
        raise DriveError(f"Could not look up Drive folder {value!r}: {exc}") from exc

    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    try:
        response = (
            service.files()
            .list(
                q="mimeType = 'application/vnd.google-apps.folder' "
                  f"and name = '{escaped}' and trashed = false",
                spaces="drive",
                fields="files(id, name)",
                pageSize=10,
            )
            .execute()
        )
    except Exception as exc:
        raise DriveError(f"Could not search for folder {value!r}: {exc}") from exc

    folders = response.get("files", [])
    if not folders:
        log.warning(
            "No Drive folder is named %r and it is not a folder ID; searching "
            "all of Drive for Takeout archives instead.", value
        )
        return None
    if len(folders) > 1:
        log.warning(
            "%d Drive folders are named %r; using the first. Set "
            "takeout.drive_folder_id to the folder ID to disambiguate.",
            len(folders), value,
        )
    return folders[0]["id"]


def list_takeout_archives(creds, folder_id: str | None = None) -> list[dict]:
    """Takeout .zip archives visible to this account, newest first.

    Discovery is by name pattern rather than a hardcoded folder ID, because
    Takeout drops archives wherever the user pointed it. folder_id narrows
    the search when config sets takeout.drive_folder_id.

    Returns dicts of {id, name, modifiedTime, size}.
    """
    google = _google_modules()
    service = _service(creds)

    clauses = [
        "trashed = false",
        f"name contains '{config.TAKEOUT_NAME_PATTERN}'",
    ]
    if folder_id:
        clauses.append(f"'{folder_id}' in parents")
    query = " and ".join(clauses)

    found: list[dict] = []
    page_token = None
    try:
        while True:
            response = (
                service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name, modifiedTime, size, mimeType)",
                    pageSize=PAGE_SIZE,
                    orderBy="modifiedTime desc",
                    pageToken=page_token,
                )
                .execute()
            )
            found.extend(response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except google["HttpError"] as exc:
        raise DriveError(f"Drive rejected the archive listing: {exc}") from exc
    except Exception as exc:
        raise DriveError(f"Could not list Drive files: {exc}") from exc

    # Takeout can deliver .tgz too; only .zip is supported by the extractor.
    archives = [f for f in found if f.get("name", "").lower().endswith(".zip")]
    skipped = len(found) - len(archives)
    if skipped:
        log.info(
            "Skipped %d non-zip Takeout file(s); set the export format to "
            ".zip in Takeout for these to be ingested.", skipped
        )
    return archives


MEDIA_URL = "https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"


def _authorized_session(creds):
    """A requests session bound to these credentials.

    Deliberately not googleapiclient's MediaIoBaseDownload, which rides on
    httplib2 and offers no usable control over per-read timeouts. Media
    downloads are the one call here that is unreliable in practice (see the
    DRIVE_* notes in config.py), so they get an HTTP stack whose timeout and
    retry behaviour can actually be set. Listing and metadata still go
    through googleapiclient, where they work fine.
    """
    google = _google_modules()
    from google.auth.transport.requests import AuthorizedSession

    session = AuthorizedSession(creds)
    # Explicit gzip: measurably more likely to get a response at all on a
    # network that stalls identity-encoded binary downloads.
    session.headers.update({"Accept-Encoding": "gzip"})
    _ = google  # keep the import-failure path shared with the rest of the module
    return session


def _download(creds, file_id: str, sink, label: str) -> int:
    """Stream one file into `sink`, retrying a stalled connection.

    Returns the number of bytes written. Each attempt restarts the transfer
    from zero, which is fine for the archive sizes involved and avoids the
    complexity of range resumption for a failure mode that is usually a dead
    connection rather than a partial one.
    """
    session = _authorized_session(creds)
    url = MEDIA_URL.format(file_id=file_id)
    timeout = (config.DRIVE_CONNECT_TIMEOUT_S, config.DRIVE_READ_TIMEOUT_S)
    last_error = None

    for attempt in range(1, config.DRIVE_DOWNLOAD_ATTEMPTS + 1):
        try:
            with session.get(url, timeout=timeout, stream=True) as response:
                if response.status_code != 200:
                    detail = response.text[:200]
                    if response.status_code in (401, 403):
                        raise DriveError(
                            f"Drive refused to send {label} "
                            f"(HTTP {response.status_code}): {detail}"
                        )
                    raise IOError(f"HTTP {response.status_code}: {detail}")
                written = 0
                sink.seek(0)
                sink.truncate()
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if chunk:
                        sink.write(chunk)
                        written += len(chunk)
                if written == 0:
                    raise IOError("the connection returned zero bytes")
                return written
        except DriveError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt < config.DRIVE_DOWNLOAD_ATTEMPTS:
                delay = config.DRIVE_DOWNLOAD_BACKOFF_S * attempt
                log.warning(
                    "Download of %s stalled (attempt %d/%d: %s); retrying in %.0fs.",
                    label, attempt, config.DRIVE_DOWNLOAD_ATTEMPTS, exc, delay,
                )
                time.sleep(delay)

    raise DriveError(
        f"Download of {label} failed after {config.DRIVE_DOWNLOAD_ATTEMPTS} "
        f"attempts: {last_error}. Drive metadata calls work but file "
        "downloads stall on some networks (a VPN or a filtering proxy is the "
        "usual cause). Try another network, or download the archive by hand "
        "and ingest it with: python -m app.sync --import <archive.zip>"
    )


def download_archive(creds, file_id: str, dest: Path) -> Path:
    """Download one archive to dest. Caller owns cleanup of the temp file."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(dest, "wb") as handle:
            _download(creds, file_id, handle, label=dest.name or file_id)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


def download_archive_to_memory(creds, file_id: str) -> io.BytesIO:
    """Download into memory, for archives small enough to avoid touching disk."""
    buffer = io.BytesIO()
    _download(creds, file_id, buffer, label=file_id)
    buffer.seek(0)
    return buffer
