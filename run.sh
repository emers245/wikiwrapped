#!/usr/bin/env bash
#
# One-line launcher for the Wikipedia Wrapped dashboard.
#
#   ./run.sh                 start the server and open the dashboard
#   ./run.sh --port 8080     use a different port
#   ./run.sh --no-browser    start the server only
#
# Finds the conda environment without needing "conda activate" first, waits
# until the API answers, then opens a browser and hands the terminal back to
# uvicorn so Ctrl-C stops the server.
#
# If the port is already serving this app (a window left open from earlier),
# the existing server is reused rather than failing on a bind error.

set -euo pipefail

ENV_NAME="wikipedia-wrapped"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8000
OPEN_BROWSER=1

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
Launch the Wikipedia Wrapped dashboard.

Usage: ./run.sh [--port N] [--no-browser]

  --port N        port to serve on (default 8000)
  --no-browser    start the server without opening a browser
  -h, --help      show this message
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)
            [ $# -ge 2 ] || { echo "--port needs a number" >&2; exit 2; }
            PORT="$2"
            shift 2
            ;;
        --no-browser)
            OPEN_BROWSER=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Locate the environment's interpreter
# ---------------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

URL="http://localhost:$PORT"

# True when $URL/health answers and identifies as this app. Written in Python
# so the script needs no curl, and so a stray server on the port is not
# mistaken for ours.
is_our_server() {
    "$PYTHON" - "$URL/health" <<'PYEOF'
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=1) as response:
        payload = json.load(response)
except Exception:
    sys.exit(1)
sys.exit(0 if "reference_space" in payload else 1)
PYEOF
}

wait_for_server() {
    local attempt
    for attempt in $(seq 1 60); do
        if is_our_server; then
            return 0
        fi
        # Stop early if the server died during startup.
        if [ -n "${SERVER_PID:-}" ] && ! kill -0 "$SERVER_PID" 2>/dev/null; then
            return 1
        fi
        sleep 0.5
    done
    return 1
}

open_browser() {
    if [ "$OPEN_BROWSER" -eq 0 ]; then
        return
    fi
    if command -v open >/dev/null 2>&1; then          # macOS
        open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then    # Linux
        xdg-open "$URL" >/dev/null 2>&1
    else
        echo "Open $URL in your browser."
    fi
}

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

cd "$PROJECT_ROOT"

if [ ! -f data/app.db ]; then
    echo "Note: data/app.db does not exist yet. The dashboard will load but"
    echo "stay empty until you ingest a history export (see the README)."
    echo
fi

if is_our_server; then
    echo "Wikipedia Wrapped is already running at $URL - reusing it."
    open_browser
    exit 0
fi

echo "Starting Wikipedia Wrapped at $URL (Ctrl-C to stop)"
"$PYTHON" -m uvicorn app.api:app --port "$PORT" &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' INT TERM EXIT

if ! wait_for_server; then
    echo "Server did not come up on port $PORT. Is the port already in use?" >&2
    exit 1
fi

open_browser
wait "$SERVER_PID"
