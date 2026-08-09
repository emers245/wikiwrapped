#!/usr/bin/env python3
"""Interactive first-run setup for the Google Takeout integration (D10).

    python scripts/setup_takeout.py                  # interactive setup
    python scripts/setup_takeout.py --status         # show current config
    python scripts/setup_takeout.py --authorize EMAIL  # re-authorize one account
    python scripts/setup_takeout.py --disable        # turn the integration off

Idempotent and re-runnable by design: it loads config.local.toml, changes
only what you answer, and writes it back. Running it again to add a second
account or flip a preference never wipes existing settings, tokens, or data.

What it never does: ask for a Google password. Authorization happens in your
browser against Google, and the only thing that comes back is a read-only
OAuth token (drive.readonly), cached under tokens/ with owner-only
permissions. Both that directory and config.local.toml are git-ignored.

The Google Cloud Console steps you have to do by hand first are in the
README under "Google Takeout integration".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config                                    # noqa: E402
from app import drive, localconfig               # noqa: E402
from extract import extract_history              # noqa: E402


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


def show_status(settings: dict) -> None:
    takeout = settings.get("takeout", {})
    accounts = localconfig.account_emails(settings)
    secret = localconfig.client_secret_path(settings)

    print(f"Config file:      {config.LOCAL_CONFIG_PATH}"
          f"{'' if Path(config.LOCAL_CONFIG_PATH).exists() else '  (not created yet)'}")
    print(f"Takeout enabled:  {bool(takeout.get('enabled'))}")
    print(f"Sync on launch:   {bool(takeout.get('sync_on_launch'))}")
    print(f"Min interval:     {localconfig.min_sync_interval_hours(settings):g} hours")
    print(f"Client secret:    {secret if secret else 'NOT FOUND'}")
    print(f"Drive folder:     "
          f"{localconfig.drive_folder_id(settings) or 'search all of Drive'}")
    print(f"Google libraries: "
          f"{'installed' if drive.libraries_available() else 'NOT INSTALLED'}")

    chosen = localconfig.chrome_profiles(settings)
    available = extract_history.find_chrome_profiles()
    if chosen:
        matched = extract_history.find_chrome_profiles(only=chosen)
        label = ", ".join(
            f"{p['display_name']} [{p['directory']}]" for p in matched
        ) or "NONE MATCHED"
        print(f"Chrome profiles:  {label}  (of {len(available)} available)")
    else:
        print(f"Chrome profiles:  all ({len(available)} found)")

    if accounts:
        print("Authorized accounts:")
        for email in accounts:
            token = localconfig.token_path(email)
            print(f"  - {email}  "
                  f"({'token present' if token.exists() else 'TOKEN MISSING, re-authorize'})")
    else:
        print("Authorized accounts: none")
    warn_if_incomplete(settings)


def authorize_one(settings: dict, email_hint: str | None = None) -> str | None:
    """Run the browser OAuth flow and file the token. Returns the email."""
    try:
        email = drive.authorize_account(settings, email_hint=email_hint)
    except drive.DriveError as exc:
        print(f"\nAuthorization failed: {exc}\n")
        return None
    except KeyboardInterrupt:
        print("\nAuthorization cancelled.\n")
        return None
    localconfig.add_account(settings, email)
    print(f"Authorized {email}. Token cached at {localconfig.token_path(email)}")
    return email


def interactive(settings: dict) -> dict:
    print("Wikipedia Wrapped: Google Takeout setup")
    print("=" * 42)
    print(
        "\nTakeout exports your Chrome history at the ACCOUNT level, so it\n"
        "reaches past Chrome's 90-day local limit and includes your phone\n"
        "and any second Google account. This is added alongside the local\n"
        "Chrome reader, which keeps working either way.\n"
    )
    show_status(settings)
    print()

    choose_profiles(settings)

    enabled = ask_yes_no(
        "\nEnable the Google Takeout integration?",
        default=localconfig.takeout_enabled(settings),
    )
    settings.setdefault("takeout", {})["enabled"] = enabled

    if not enabled:
        settings["takeout"]["sync_on_launch"] = False
        print("\nTakeout disabled. The local Chrome path is unaffected.")
        return settings

    if not drive.libraries_available():
        print(f"\nThe Google libraries are missing.\n{drive.INSTALL_HINT}\n"
              "Settings will still be saved; install them, then re-run this.")

    if localconfig.client_secret_path(settings) is None:
        print(
            "\nNo OAuth client secret found.\n"
            f"Download the Desktop app client JSON from Google Cloud Console\n"
            f"into {config.PROJECT_ROOT}\n"
            f"(the filename matches {config.CLIENT_SECRET_GLOB}, which is\n"
            "git-ignored). The README lists the console steps.\n"
        )
        path = input("Path to the client secret JSON (blank to skip): ").strip()
        if path:
            settings["takeout"]["client_secret"] = path

    # Authorize: existing accounts first, then as many new ones as wanted.
    existing = localconfig.account_emails(settings)
    if existing:
        print(f"\nAlready authorized: {', '.join(existing)}")
    if drive.libraries_available() and localconfig.client_secret_path(settings):
        prompt = ("Authorize another Google account?" if existing
                  else "Authorize a Google account now?")
        while ask_yes_no("\n" + prompt, default=not existing):
            if authorize_one(settings) is None:
                if not ask_yes_no("Try again?", default=False):
                    break
            prompt = "Authorize another Google account?"

    print(
        "\nCheck-on-launch sync makes ./run.sh look for a new Takeout archive\n"
        "before starting the dashboard. It is a single cheap pass, it never\n"
        "blocks startup, and it early-exits if it ran recently."
    )
    settings["takeout"]["sync_on_launch"] = ask_yes_no(
        "Sync on launch?", default=settings["takeout"].get("sync_on_launch", True)
    )
    if settings["takeout"]["sync_on_launch"]:
        print(
            "\nTakeout only produces a new archive every ~2 months, so there\n"
            "is nothing to gain from checking often."
        )
        settings["takeout"]["min_sync_interval_hours"] = ask_number(
            "Minimum hours between syncs?",
            localconfig.min_sync_interval_hours(settings),
        )

    current_folder = localconfig.drive_folder_id(settings) or ""
    print(
        "\nOptionally restrict the search to one Drive folder. A folder NAME\n"
        "(like Takeout) or the ID from its URL both work; blank searches all\n"
        "of Drive, which is usually what you want."
    )
    folder = input(
        f"Drive folder name or ID [{current_folder or 'search all'}]: "
    ).strip()
    if folder:
        settings["takeout"]["drive_folder_id"] = folder

    return settings


def choose_profiles(settings: dict) -> dict:
    """Pick which local Chrome profiles to read. Defaults to all of them."""
    profiles = extract_history.find_chrome_profiles()
    if not profiles:
        print("\nNo local Chrome profile with a History database was found.")
        return settings

    print("\nLocal Chrome profiles")
    print("-" * 42)
    counts = {}
    for index, profile in enumerate(profiles, 1):
        try:
            rows = extract_history.load_chrome_db(profile["path"])
            _visits, stats = extract_history.build_visits(rows)
            kept = stats["kept"]
        except Exception:
            kept = None
        counts[profile["directory"]] = kept
        shown = "unreadable" if kept is None else f"{kept} Wikipedia visits"
        account = f"  {profile['account']}" if profile["account"] else ""
        print(f"  {index}. {profile['display_name']} "
              f"[{profile['directory']}]{account}\n     {shown}")

    selected = localconfig.chrome_profiles(settings)
    if selected:
        print(f"\nCurrently reading only: {', '.join(selected)}")
    else:
        print("\nCurrently reading: all profiles")

    if ask_yes_no("\nRead all profiles?", default=not selected):
        settings.setdefault("local", {})["chrome_profiles"] = []
        return settings

    print("\nEnter the numbers to include, separated by commas (e.g. 2,3).")
    while True:
        answer = input("Profiles to include: ").strip()
        if not answer:
            print("Nothing selected; keeping all profiles.")
            settings.setdefault("local", {})["chrome_profiles"] = []
            return settings
        try:
            picked = [
                profiles[int(part) - 1]
                for part in answer.replace(" ", "").split(",")
                if part
            ]
        except (ValueError, IndexError):
            print(f"Please enter numbers between 1 and {len(profiles)}.")
            continue
        # Store the directory name: display names can be renamed in Chrome.
        settings.setdefault("local", {})["chrome_profiles"] = [
            profile["directory"] for profile in picked
        ]
        total = sum(
            counts.get(p["directory"]) or 0 for p in picked
        )
        print("Reading: "
              + ", ".join(f"{p['display_name']} [{p['directory']}]" for p in picked)
              + f"  ({total} Wikipedia visits)")
        return settings


def warn_if_incomplete(settings: dict) -> None:
    """Say plainly when the saved config cannot actually sync anything.

    Enabling Takeout without finishing authorization is easy to do (the OAuth
    flow can be cancelled or fail), and it produces a config that looks set up
    but does nothing except warn on every sync.
    """
    if not localconfig.takeout_enabled(settings):
        return
    if localconfig.account_emails(settings):
        return
    print(
        "\n" + "!" * 62
        + "\nTakeout is ENABLED but NO ACCOUNT IS AUTHORIZED, so syncing will"
        + "\nfind nothing and warn every time. Authorize an account with:"
        + "\n    python scripts/setup_takeout.py --authorize you@example.com"
        + "\nor turn the integration off with:"
        + "\n    python scripts/setup_takeout.py --disable"
        + "\n" + "!" * 62
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up the read-only Google Takeout integration."
    )
    parser.add_argument("--status", action="store_true",
                        help="Print the current configuration and exit")
    parser.add_argument("--authorize", metavar="EMAIL", default=None,
                        help="Re-run the OAuth flow for one account")
    parser.add_argument("--disable", action="store_true",
                        help="Turn the integration off (keeps tokens and data)")
    args = parser.parse_args(argv)

    settings = localconfig.load()

    if args.status:
        show_status(settings)
        return 0

    if args.disable:
        settings.setdefault("takeout", {})["enabled"] = False
        settings["takeout"]["sync_on_launch"] = False
        path = localconfig.save(settings)
        print(f"Takeout integration disabled. Wrote {path}")
        print("Tokens and the visit log were left untouched.")
        return 0

    if args.authorize:
        if authorize_one(settings, email_hint=args.authorize) is None:
            return 1
        settings.setdefault("takeout", {})["enabled"] = True
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
    print("  python -m app.sync --force     sync now")
    print("  ./run.sh                       launch (syncs first)")
    print("  ./run.sh --no-sync             launch without syncing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
