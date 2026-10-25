#!/usr/bin/env bash
#
# Developer shortcut for the Wikipedia Wrapped launcher (app/launcher.py).
#
#   ./run.sh                 open the app window
#   ./run.sh --browser       open in a normal browser tab instead
#   ./run.sh --no-sync       skip the check-on-launch history sync
#   ./run.sh --force-sync    sync even if one ran recently
#   ./run.sh --no-enrich     skip topic/map enrichment of new pages
#   ./run.sh --port N        prefer a different port
#
# Everything run.sh used to do (sync, enrich, serve, open, reuse a running
# server) now lives in the cross-platform Python launcher; this script only
# finds the conda environment so "conda activate" is not needed first, then
# hands over. Kept rather than removed so existing aliases keep working.

set -euo pipefail

ENV_NAME="wikipedia-wrapped"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Calling the interpreter by path avoids "conda activate", which only works
# in a shell that has had conda's hook sourced.
find_python() {
    # Already inside the right environment.
    if [ -n "${CONDA_PREFIX:-}" ] && \
       [ "$(basename "$CONDA_PREFIX")" = "$ENV_NAME" ] && \
       [ -x "$CONDA_PREFIX/bin/python" ]; then
        echo "$CONDA_PREFIX/bin/python"
        return 0
    fi
    # Ask conda where its environments live.
    if command -v conda >/dev/null 2>&1; then
        local base
        base="$(conda info --base 2>/dev/null || true)"
        if [ -n "$base" ] && [ -x "$base/envs/$ENV_NAME/bin/python" ]; then
            echo "$base/envs/$ENV_NAME/bin/python"
            return 0
        fi
    fi
    # Common install locations, for shells where conda is not on PATH.
    local candidate
    for candidate in \
        "$HOME/miniconda3/envs/$ENV_NAME/bin/python" \
        "$HOME/anaconda3/envs/$ENV_NAME/bin/python" \
        "/opt/miniconda3/envs/$ENV_NAME/bin/python" \
        "/opt/anaconda3/envs/$ENV_NAME/bin/python"
    do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON="$(find_python)"; then
    cat >&2 <<EOF
Could not find the "$ENV_NAME" conda environment.

Build it first:
    conda env create -f environment.yml
    conda run -n $ENV_NAME python scripts/check_env.py --full
EOF
    exit 1
fi

cd "$PROJECT_ROOT"
exec "$PYTHON" -m app.launcher "$@"
