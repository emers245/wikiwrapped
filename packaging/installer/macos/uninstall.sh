#!/bin/bash
# Uninstall WikiWrapped from this Mac, keeping your data.
#
#     bash ~/Library/WikiWrapped-App/uninstall.sh
#
# Removes the program (~/Library/WikiWrapped-App) and its shortcut
# (~/Applications/WikiWrapped.app). Your history, settings and logs
# stay in ~/Library/Application Support/WikiWrapped, so reinstalling
# later picks up where you left off. To remove those too, delete that
# folder after running this (the app's Help, "Uninstalling", says more).

set -euo pipefail

INSTALL_ROOT="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/Applications/WikiWrapped.app"
OUR_BUNDLE_ID="io.github.emers245.wikiwrapped"
DATA="$HOME/Library/Application Support/WikiWrapped"

# Refuse to run from anywhere that is not an installation of this app:
# this script deletes the folder it is in.
if ! compgen -G "$INSTALL_ROOT/v*/conda-meta/wikiwrapped-*.json" > /dev/null; then
    echo "$INSTALL_ROOT does not look like a WikiWrapped installation; nothing removed." >&2
    exit 1
fi

if pgrep -f "$INSTALL_ROOT/.*/launch.py" > /dev/null; then
    echo "WikiWrapped is running. Quit it (WikiWrapped > Quit), then run this again." >&2
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
echo "WikiWrapped is uninstalled. Your data are still in:"
echo "  $DATA"
echo "Delete that folder too if you no longer want them."
