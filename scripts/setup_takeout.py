#!/usr/bin/env python3
"""Terminal setup for Wikipedia Wrapped's sources and sync preferences.

    python scripts/setup_takeout.py                  # interactive setup
    python scripts/setup_takeout.py --status         # show current settings
    python scripts/setup_takeout.py --authorize EMAIL  # re-authorize one account
    python scripts/setup_takeout.py --disable        # turn the Drive route off
    python scripts/setup_takeout.py --add-folder PATH [--label NAME]
    python scripts/setup_takeout.py --remove-folder PATH

The app window's first-run screens and Settings do the same job; this is
the terminal front end for developers. Both are thin layers over
app/setup_service.py, which holds every rule and every check, so the two
can never disagree. This file only asks questions and prints answers.

Idempotent and re-runnable by design: it loads config.local.toml, changes
only what you answer, and writes it back. Running it again to add a second
account or flip a preference never wipes existing settings, tokens, or data.

What it never does: ask for a Google password. Authorization happens in your
browser against Google, and the only thing that comes back is a read-only
OAuth token (drive.readonly), cached under tokens/ with owner-only
permissions. That directory and config.local.toml both live in the
per-user data directory (config.USER_DATA_DIR), not in the repo.

The Google Cloud Console steps for the Advanced Drive route are in the
README under "Google Takeout integration".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                    # noqa: E402
from app import localconfig, setup_service       # noqa: E402


def ask_yes_no(question: str, default: bool) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        answer = input(question + suffix).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def ask_number(question: str, default: float) -> float:
    answer = input(f"{question} [{default:g}] ").strip()
    if not answer:
        return default
    try:
        return max(0.0, float(answer))
    except ValueError:
        print(f"Not a number; keeping {default:g}.")
        return default


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------

def show_status(settings: dict) -> None:
    """Print setup_service.status() for a terminal."""
    info = setup_service.status(settings)
    paths, takeout, sync_prefs = info["paths"], info["takeout"], info["sync"]

    exists = Path(paths["settings_file"]).exists()
    print(f"Settings file:    {paths['settings_file']}"
          f"{'' if exists else '  (not created yet)'}")
    print(f"Sync on launch:   {sync_prefs['on_launch']}")
    print(f"Min interval:     {sync_prefs['min_interval_hours']:g} hours")

    profiles = setup_service.chrome_profiles(settings, count_visits=False)
    selected = [p for p in profiles if p["selected"]]
    if not info["local"]["enabled"]:
        print(f"Chrome profiles:  none (reading local Chrome is off; "
              f"{len(profiles)} found)")
    elif info["local"]["chrome_profiles"]:
        names = ", ".join(f"{p['display_name']} [{p['directory']}]" for p in selected)
        print(f"Chrome profiles:  {names or 'NONE MATCHED'}  "
              f"(of {len(profiles)} available)")
    else:
        print(f"Chrome profiles:  all ({len(profiles)} found)")

    if info["watched_folders"]:
        print("Watched folders:")
        for entry in info["watched_folders"]:
            label = f"  [{entry['label']}]" if entry["label"] else ""
            print(f"  - {entry['path']}{label}\n      {entry['message']}")
    else:
        print("Watched folders:  none")

    print(f"Drive (OAuth):    {'enabled' if takeout['enabled'] else 'disabled'}")
    print(f"Client secret:    {takeout['client_secret'] or 'NOT FOUND'}")
    print(f"Drive folder:     {takeout['drive_folder'] or 'search all of Drive'}")
    print(f"Google libraries: "
          f"{'installed' if takeout['libraries_available'] else 'NOT INSTALLED'}")
    if takeout["accounts"]:
        print("Authorized accounts:")
        for account in takeout["accounts"]:
            state = ("needs re-authorization" if account["needs_reauthorization"]
                     else "ok")
            print(f"  - {account['email']}  ({state})")
    else:
        print("Authorized accounts: none")
    warn_if_incomplete(settings)


def warn_if_incomplete(settings: dict) -> None:
    message = setup_service.incomplete_warning(settings)
    if message:
        print("\n" + "!" * 62 + "\n" + message
              + "\n    python scripts/setup_takeout.py --authorize you@example.com"
              + "\n    python scripts/setup_takeout.py --disable\n" + "!" * 62)


# ---------------------------------------------------------------------------
# Interactive steps
# ---------------------------------------------------------------------------

def authorize_one(settings: dict, email_hint: str | None = None) -> str | None:
    """Run the browser OAuth flow and file the token. Returns the email."""
    try:
        email = setup_service.authorize(settings, email_hint=email_hint)
    except setup_service.SetupError as exc:
        print(f"\nAuthorization failed: {exc}\n")
        return None
    except KeyboardInterrupt:
        print("\nAuthorization cancelled.\n")
        return None
    print(f"Authorized {email}. Token cached at {localconfig.token_path(email)}")
    return email


def choose_profiles(settings: dict) -> dict:
    """Pick which local Chrome profiles to read. Defaults to all of them."""
    profiles = setup_service.chrome_profiles(settings)
    if not profiles:
        print("\nNo local Chrome profile with a History database was found.")
        return settings

    print("\nLocal Chrome profiles")
    print("-" * 42)
    for index, profile in enumerate(profiles, 1):
        visits = profile["wikipedia_visits"]
        shown = "unreadable" if visits is None else f"{visits} Wikipedia visits"
        account = f"  {profile['account']}" if profile["account"] else ""
        print(f"  {index}. {profile['display_name']} "
              f"[{profile['directory']}]{account}\n     {shown}")

    if ask_yes_no("\nRead all profiles?",
                  default=all(p["selected"] for p in profiles)):
        return setup_service.set_profiles(settings, None)

    print("\nEnter the numbers to include, separated by commas (e.g. 2,3),")
    print("or 0 to read no local Chrome profile at all.")
    while True:
        answer = input("Profiles to include: ").strip().replace(" ", "")
        if not answer:
            print("Nothing entered; keeping all profiles.")
            return setup_service.set_profiles(settings, None)
        if answer == "0":
            return setup_service.set_profiles(settings, [])
        try:
            picked = [profiles[int(part) - 1] for part in answer.split(",") if part]
        except (ValueError, IndexError):
            print(f"Please enter numbers between 1 and {len(profiles)}, or 0.")
            continue
        # Store the directory name: display names can be renamed in Chrome.
        return setup_service.set_profiles(settings, [p["directory"] for p in picked])


def choose_folders(settings: dict) -> dict:
    """Add watched folders (Takeout zips on this computer, no sign-in)."""
    current = localconfig.watched_folders(settings)
    print(
        "\nWatched folders hold Google Takeout zips: downloaded by hand, or\n"
        "synced by Google Drive for desktop. No Google sign-in is needed."
    )
    for entry in current:
        print(f"  - {entry['path']}" + (f"  [{entry['label']}]" if entry["label"] else ""))
    while ask_yes_no("Add a watched folder?", default=not current):
        path = input("Folder path: ").strip()
        if not path:
            continue
        label = input("Label (e.g. Personal, or your Google email; optional): ").strip()
        try:
            setup_service.add_folder(settings, path, label)
        except setup_service.SetupError as exc:
            print(f"Not added: {exc}")
            continue
        print(setup_service.inspect_folder(path)["message"])
    return settings


def ask_sync_preferences(settings: dict) -> dict:
    """Sync-on-launch covers every source, so it is asked either way."""
    print(
        "\nCheck-on-launch sync reads local Chrome, any watched folders, and\n"
        "Drive (if enabled) when the app starts. It is a single cheap pass,\n"
        "it never blocks startup, and it early-exits if it ran recently.\n"
        "Chrome forgets history after about 90 days, so leaving it on is\n"
        "what keeps the local history from expiring uncaptured."
    )
    on_launch = ask_yes_no("Sync on launch?", default=localconfig.sync_on_launch(settings))
    interval = localconfig.min_sync_interval_hours(settings)
    if on_launch:
        interval = ask_number("Minimum hours between syncs?", interval)
    try:
        return setup_service.set_sync(settings, on_launch, interval)
    except setup_service.SetupError as exc:
        print(f"{exc} Keeping the previous value.")
        return settings


def choose_drive(settings: dict) -> dict:
    """The Advanced route: Drive through the user's own OAuth client."""
    enabled = ask_yes_no(
        "\nUse the Advanced Google Drive route (your own OAuth client)?",
        default=localconfig.takeout_enabled(settings),
    )
    setup_service.set_drive(settings, enabled)
    if not enabled:
        return settings

    if localconfig.client_secret_path(settings) is None:
        print(
            "\nNo OAuth client secret found. Download the Desktop app client\n"
            "JSON from Google Cloud Console (the README lists the steps)."
        )
        path = input("Path to the client secret JSON (blank to skip): ").strip()
        if path:
            try:
                setup_service.import_client_secret(settings, path)
                print(f"Copied to {config.USER_DATA_DIR}.")
            except setup_service.SetupError as exc:
                print(f"Not imported: {exc}")

    existing = localconfig.account_emails(settings)
    if existing:
        print(f"\nAlready authorized: {', '.join(existing)}")
    if localconfig.client_secret_path(settings):
        prompt = ("Authorize another Google account?" if existing
                  else "Authorize a Google account now?")
        while ask_yes_no("\n" + prompt, default=not existing):
            if authorize_one(settings) is None and not ask_yes_no(
                    "Try again?", default=False):
                break
            prompt = "Authorize another Google account?"

    current = localconfig.drive_folder_id(settings) or ""
    print(
        "\nOptionally restrict the search to one Drive folder. A folder NAME\n"
        "(like Takeout) or the ID from its URL both work; blank searches all\n"
        "of Drive, which is usually what you want."
    )
    folder = input(f"Drive folder name or ID [{current or 'search all'}]: ").strip()
    if folder:
        setup_service.set_drive(settings, True, folder)
    return settings


def interactive(settings: dict) -> dict:
    print("Wikipedia Wrapped: setup")
    print("=" * 42)
    print(
        "\nLocal Chrome history is read directly. Google Takeout adds history\n"
        "at the ACCOUNT level, so it reaches past Chrome's 90-day limit and\n"
        "includes your phone and any second Google account.\n"
    )
    show_status(settings)
    choose_profiles(settings)
    choose_folders(settings)
    choose_drive(settings)
    ask_sync_preferences(settings)
    return settings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up Wikipedia Wrapped's history sources and sync."
    )
    parser.add_argument("--status", action="store_true",
                        help="Print the current settings and exit")
    parser.add_argument("--authorize", metavar="EMAIL", default=None,
                        help="Re-run the OAuth flow for one account")
    parser.add_argument("--disable", action="store_true",
                        help="Turn the Drive integration off (keeps tokens and data)")
    parser.add_argument("--add-folder", metavar="PATH", default=None,
                        help="Watch a folder for Takeout zips (no sign-in needed)")
    parser.add_argument("--label", default="",
                        help="Label for --add-folder, e.g. Personal or an email")
    parser.add_argument("--remove-folder", metavar="PATH", default=None,
                        help="Stop watching a folder (its zips are left alone)")
    args = parser.parse_args(argv)

    settings = localconfig.load()

    if args.status:
        show_status(settings)
        return 0

    if args.add_folder or args.remove_folder:
        try:
            if args.add_folder:
                setup_service.add_folder(settings, args.add_folder, args.label)
            if args.remove_folder:
                localconfig.remove_folder(settings, args.remove_folder)
        except setup_service.SetupError as exc:
            print(exc, file=sys.stderr)
            return 1
        path = localconfig.save(settings)
        print(f"Wrote {path}")
        show_status(settings)
        return 0

    if args.disable:
        setup_service.set_drive(settings, False)
        path = localconfig.save(settings)
        print(f"Drive integration disabled. Wrote {path}")
        print("Tokens and the visit log were left untouched.")
        return 0

    if args.authorize:
        if authorize_one(settings, email_hint=args.authorize) is None:
            return 1
        path = localconfig.save(settings)
        print(f"Wrote {path}")
        return 0

    try:
        settings = interactive(settings)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled; nothing was written.")
        return 1

    path = localconfig.save(settings)
    print(f"\nWrote {path}")
    warn_if_incomplete(settings)
    print("\nNext:")
    print("  python -m app.sync --force         sync now")
    print("  python -m app.launcher             launch (syncs in the background)")
    print("  python -m app.launcher --no-sync   launch without syncing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
