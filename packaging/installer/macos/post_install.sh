#!/bin/bash
# Runs inside the macOS installer once the packages and the shortcut are
# installed into this version's folder ($PREFIX, for example
# ~/Library/WikiWrapped-App/v0.1.0). Runs as the user, never as root.
# Any failure here fails the installation, with this output in the
# installer log (Window > Installer Log).

set -euo pipefail

PY="$PREFIX/bin/python"
INSTALL_ROOT="$(dirname "$PREFIX")"

# 1. pywebview (the app window) and its macOS dependencies: wheels shipped
#    inside the installer, installed offline and only if each one matches
#    its sha256 in packaging/wheels/requirements-<platform>.txt. conda-forge
#    has no macOS build of pywebview (packaging/README.md). The user's own
#    pip settings and user site-packages must not interfere.
echo "Installing the app window library (pywebview)"
PIP_CONFIG_FILE=/dev/null PYTHONNOUSERSITE=1 "$PY" -m pip install \
    --no-index --no-deps --require-hashes --disable-pip-version-check \
    --no-warn-script-location --quiet \
    --find-links "$PREFIX/wheels" -r "$PREFIX/wheels/requirements.txt"
rm -rf "$PREFIX/wheels"

# 2. Compile the app's code now: the app never writes bytecode into its
#    install folder at runtime (packaging/launch.py), so this is the only
#    chance, and it makes every launch start a little faster.
echo "Compiling the app"
"$PY" -m compileall -q -j 0 "$PREFIX/share/wikiwrapped" > /dev/null

# 3. Load every library once. The first time any program loads a newly
#    installed library, macOS checks it, and for this app's few hundred
#    libraries that took 20 s before the window could appear, and two more
#    minutes before the first map update, on the Intel Mac it was measured
#    on (2026-10-25). The result is remembered, so paying it here, while
#    the installer is still showing progress, makes the first launch as
#    quick as any other. Nothing is written into the install folder:
#    no bytecode (-B), and numba's compile cache goes to a temporary folder.
#    Only a speed-up, so a failure does not fail the installation (whether
#    the app works is what `launch.py --self-test` checks).
echo "Preparing the app's libraries for their first use (a minute or two)"
WARMUP_CACHE="$(mktemp -d)"
(cd "$PREFIX/share/wikiwrapped" && NUMBA_CACHE_DIR="$WARMUP_CACHE" \
    "$PY" -B -c "from app.selftest import check_imports; check_imports()") \
    || echo "Warning: could not preload the libraries; the first launch will be slower."
rm -rf "$WARMUP_CACHE"

# 4. The uninstaller, at a path that does not change between versions.
cp "$PREFIX/uninstall.sh" "$INSTALL_ROOT/uninstall.sh"
chmod +x "$INSTALL_ROOT/uninstall.sh"

# 5. Upgrades: each version installs into its own folder (the installer
#    refuses to install into an existing one), so remove the others now
#    that this one is complete. Only folders that are an installation of
#    this app are touched, and user data never live in here.
for old in "$INSTALL_ROOT"/v*; do
    [[ -d "$old" && "$old" != "$PREFIX" ]] || continue
    if compgen -G "$old/conda-meta/wikiwrapped-*.json" > /dev/null; then
        echo "Removing the previous version: $old"
        rm -rf "$old"
    fi
done
