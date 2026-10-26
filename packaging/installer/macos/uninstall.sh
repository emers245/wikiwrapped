#!/bin/bash
# Uninstall Wikipedia Wrapped from this Mac, keeping your data.
#
#     bash ~/Library/WikipediaWrapped-App/uninstall.sh
#
# Removes the program (~/Library/WikipediaWrapped-App) and its shortcut
# (~/Applications/Wikipedia Wrapped.app). Your history, settings and logs
# stay in ~/Library/Application Support/WikipediaWrapped, so reinstalling
# later picks up where you left off. To remove those too, delete that
# folder after running this (the app's Help, "Uninstalling", says more).

set -euo pipefail

INSTALL_ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/Wikipedia Wrapped.app"
OUR_BUNDLE_ID="io.github.emers245.wikipediawrapped"
DATA="$HOME/Library/Application Support/WikipediaWrapped"

# Refuse to run from anywhere that is not an installation of this app:
# this script deletes the folder it is in.
if ! compgen -G "$INSTALL_ROOT/v*/conda-meta/wikipedia-wrapped-*.json" > /dev/null; then
    echo "$INSTALL_ROOT does not look like a Wikipedia Wrapped installation; nothing removed." >&2
    exit 1
fi

if pgrep -f "$INSTALL_ROOT/.*/launch.py" > /dev/null; then
    echo "Wikipedia Wrapped is running. Quit it (Wikipedia Wrapped > Quit), then run this again." >&2
    exit 1
fi

if [[ -d "$APP" ]]; then
    bundle_id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" \
        "$APP/Contents/Info.plist" 2>/dev/null || true)
    if [[ "$bundle_id" == "$OUR_BUNDLE_ID" ]]; then
        rm -rf "$APP"
        echo "Removed $APP"
    fi
fi

rm -rf "$INSTALL_ROOT"
echo "Removed $INSTALL_ROOT"
echo
echo "Wikipedia Wrapped is uninstalled. Your data are still in:"
echo "  $DATA"
echo "Delete that folder too if you no longer want them."
