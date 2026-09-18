#!/usr/bin/env bash
#
# runlocal.sh - set up a local virtualenv and run acog for testing.
#
# Creates/uses a .venv, installs requirements, and launches the app with
# development-friendly defaults. Every setting can be overridden by exporting
# the corresponding environment variable before invoking this script, e.g.:
#
#   UPSTREAM_URL=https://galaxy.ansible.com ./runlocal.sh
#   PORT=8080 PUBLISH_ENABLED=false ./runlocal.sh
#   SERVER=flask ./runlocal.sh          # use Flask's dev server + reloader
#
# Any extra arguments are passed through to the underlying server command.

set -euo pipefail

cd "$(dirname "$0")"

#VENV="${VENV:-.venv}"
#PYTHON="${PYTHON:-python3}"

# --- app configuration (see src/config.py) ----------------------------------
export ARTIFACT_DIR="${ARTIFACT_DIR:-$(pwd)/artifacts}"
export PORT="${PORT:-5000}"
export UI_ENABLED="${UI_ENABLED:-true}"
export PUBLISH_ENABLED="${PUBLISH_ENABLED:-true}"
export RELATIVE_URLS="${RELATIVE_URLS:-false}"
export SCOG_PUBLISH_API_KEYS="${SCOG_PUBLISH_API_KEYS:-localdevkey}"
# Default to a pull-through cache in front of galaxy.ansible.com (matching the
# container image default). Set UPSTREAM_URL="" to run as a local-only mirror.
export UPSTREAM_URL="${UPSTREAM_URL-https://galaxy.ansible.com}"
#export REQUESTS_CA_BUNDLE=/etc/pki/tls/certs/ca-bundle.crt

SERVER="${SERVER:-gunicorn}"

mkdir -p "$ARTIFACT_DIR"

echo ">> artifacts dir : $ARTIFACT_DIR"
echo ">> listening on  : http://127.0.0.1:${PORT}"
echo ">> UI enabled    : $UI_ENABLED"
echo ">> publish keys  : $SCOG_PUBLISH_API_KEYS"
echo ">> upstream      : ${UPSTREAM_URL:-<none>}"
echo

if [ "$SERVER" = "flask" ]; then
    export FLASK_APP=wsgi:app
    exec flask run --host 0.0.0.0 --port "$PORT" --debug "$@"
else
    exec gunicorn \
        --bind "0.0.0.0:${PORT}" \
        --workers "${GUNICORN_WORKERS:-2}" \
        --threads "${GUNICORN_THREADS:-4}" \
        --timeout "${GUNICORN_TIMEOUT:-120}" \
        --reload \
        --access-logfile - \
        --error-logfile - \
        wsgi:app "$@"
fi
