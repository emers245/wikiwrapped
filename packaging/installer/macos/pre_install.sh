#!/bin/bash
# Runs inside the macOS installer, after the packages are unpacked into the
# new version's folder ($PREFIX) and before they are installed and the
# shortcut is made. Runs as the user (a "just me" install), never as root.
#
# Makes way for the shortcut: menuinst refuses to create
# ~/Applications/Wikipedia Wrapped.app when one exists, which it does after
# any earlier install. The old version's folder is removed later, by
# post_install.sh, once this version is known to be in place.

set -euo pipefail

APP="$HOME/Applications/Wikipedia Wrapped.app"
OUR_BUNDLE_ID="io.github.emers245.wikipediawrapped"

if [[ -d "$APP" ]]; then
    # Only ever our own shortcut: another app of the same name is left
    # alone (and the install then stops, with menuinst's message, rather
    # than deleting something that is not ours).
    bundle_id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" \
        "$APP/Contents/Info.plist" 2>/dev/null || true)
    if [[ "$bundle_id" == "$OUR_BUNDLE_ID" ]]; then
        echo "Removing the previous version's shortcut: $APP"
        rm -rf "$APP"
    else
        echo "Leaving $APP alone: it is not Wikipedia Wrapped's ($bundle_id)"
    fi
fi
