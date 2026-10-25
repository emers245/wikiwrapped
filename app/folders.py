"""Find Takeout archives in folders on this computer (watched folders).

The no-sign-in route to Google Takeout. The user points the app at one or
more folders, and each sync looks in them for Takeout zips. The zips get
there in one of two ways:

- by hand: the user downloads each archive from Takeout into the folder, or
- through Google Drive for desktop, which mirrors the Drive folder Takeout
  delivers into, so a scheduled export lands on disk without the user (or
  this app) ever touching Google's APIs.

This module is the local-disk counterpart of drive.py: discovery and file
metadata only. It never parses an archive (extract/extract_history.py owns
every browser format, repo_spec D1), and it never modifies, moves, copies,
or deletes anything it finds. Archives are read in place by the sync.

Google Drive for desktop can expose "online-only" files: placeholders whose
contents are fetched from the cloud the first time something reads them.
Listing a folder and stat()-ing a file never trigger that download, which is
why everything here works from metadata alone and why the idempotency key
(archive_key) is built from stat() rather than a content hash. Reading a
placeholder is the sync's job, and may be slow or fail offline; that is an
expected, recorded failure there, not an error here.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import config

# How far below a watched folder to look. Takeout's Drive folder is flat, so
# 1 would do for the common case; a little more covers a user who picks the
# parent ("My Drive") or keeps archives in dated subfolders, without walking
# an entire synced Drive on every launch.
MAX_DEPTH = 4

# macOS marks a File Provider placeholder whose data are not on disk with the
# SF_DATALESS flag in st_flags. Python's stat module only names it from 3.13,
# so the value from <sys/stat.h> is spelled out here.
_SF_DATALESS = 0x40000000

# Windows cloud-file attributes in st_file_attributes, from winnt.h:
# OFFLINE, RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS. Any of them means "the
# contents are not local yet".
_WIN_CLOUD_ATTRIBUTES = 0x00001000 | 0x00040000 | 0x00400000


class FolderUnavailable(OSError):
    """The watched folder itself is missing or cannot be listed.

    status is "missing" or "unreadable", which the sync report and /health
    show per folder.
    """

    def __init__(self, status: str, message: str):
        self.status = status
        super().__init__(message)


@dataclass
class ScanResult:
    zips: list[Path] = field(default_factory=list)
    # Subfolders that could not be listed. Reported, but they do not make
    # the folder as a whole unavailable.
    errors: list[str] = field(default_factory=list)


def is_takeout_zip_name(name: str) -> bool:
    """Takeout names archives "takeout-YYYYMMDDTHHMMSSZ-<nnn>.zip".

    Matched case-insensitively on the prefix (config.TAKEOUT_NAME_PATTERN)
    so a renamed-but-recognizable file still lands, and on the .zip suffix
    because the extractor reads only zips (Takeout can also emit .tgz).
    """
    lower = name.lower()
    return lower.startswith(config.TAKEOUT_NAME_PATTERN) and lower.endswith(".zip")


def scan(folder: str | Path, max_depth: int = MAX_DEPTH) -> ScanResult:
    """Every Takeout zip under folder, at most max_depth levels down.

    Hidden directories are skipped (".Trash", ".tmp.drivedownload", and the
    like), symlinks to directories are not followed (no cycles), and files
    still being written by a browser (".crdownload", ".part") never match
    the .zip suffix. Results are resolved paths, sorted, deduped.

    Raises FolderUnavailable when the folder itself is missing or
    unreadable; trouble below it is collected in ScanResult.errors.
    """
    root = Path(folder).expanduser()
    if not root.exists():
        raise FolderUnavailable(
            "missing",
            f"Folder not found: {root}. If it is on an external or network "
            "drive, connect it; if it moved, update it in Settings.",
        )
    if not root.is_dir():
        raise FolderUnavailable("unreadable", f"Not a folder: {root}")
    try:
        os.listdir(root)
    except OSError as exc:
        raise FolderUnavailable("unreadable", _unreadable_message(root, exc)) from exc

    result = ScanResult()
    found: set[Path] = set()
    root_depth = len(root.resolve().parts)

    def on_error(exc: OSError) -> None:
        result.errors.append(f"Could not look inside {exc.filename}: {exc.strerror}")

    for current, dirs, files in os.walk(root.resolve(), onerror=on_error):
        depth = len(Path(current).parts) - root_depth
        # Prune in place: os.walk honors edits to dirs when topdown=True.
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and depth < max_depth
        )
        for name in files:
            if is_takeout_zip_name(name):
                found.add((Path(current) / name).resolve())
    result.zips = sorted(found)
    return result


def archive_key(path: Path, stat: os.stat_result | None = None) -> tuple[str, str]:
    """(file_id, modified_time) for the processed_archives table.

    The Drive route keys on Drive's file ID and modifiedTime; this is the
    same idea for a local file:

    - file_id "file:<resolved path>". The prefix keeps it from ever
      colliding with a Drive ID, and resolving makes two spellings of the
      same path (a symlinked folder, a trailing "..") one key.
    - modified_time "<mtime ISO-8601>|size=<bytes>". A zip that is replaced
      or re-downloaded changes one or both, and is then read once more.

    Why not a content hash: hashing reads the whole file, which for a Drive
    online-only placeholder means downloading it, on every sync. stat()
    costs nothing and never triggers a download. The price is that a copy
    of an archive under a second path is read once more; that is harmless,
    because dedup (the visit log's exact key, then the database's
    UNIQUE (page_id, timestamp)) is what actually prevents duplicate visits.
    This key only avoids re-reading.
    """
    stat = stat or path.stat()
    mtime = dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
    stamp = mtime.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return f"file:{path.resolve().as_posix()}", f"{stamp}|size={stat.st_size}"


def looks_cloud_only(stat: os.stat_result) -> bool:
    """Best-effort: is this a cloud placeholder whose data are not local?

    Used only to make a read failure's message helpful ("make the folder
    available offline"), never to decide whether to try reading: a
    placeholder that is online simply downloads when read, which is fine.
    False when the platform gives no signal.
    """
    flags = getattr(stat, "st_flags", 0) or 0
    if sys.platform == "darwin" and flags & _SF_DATALESS:
        return True
    attributes = getattr(stat, "st_file_attributes", 0) or 0
    return bool(attributes & _WIN_CLOUD_ATTRIBUTES)


def _unreadable_message(root: Path, exc: OSError) -> str:
    message = f"Cannot read the folder {root}: {exc.strerror or exc}."
    if sys.platform == "darwin" and isinstance(exc, PermissionError):
        # macOS privacy controls (TCC) guard Desktop, Documents, Downloads
        # and removable volumes per app, and a denied prompt looks exactly
        # like a permission error.
        message += (
            " On macOS, allow access in System Settings > Privacy & Security "
            "> Files and Folders."
        )
    return message
