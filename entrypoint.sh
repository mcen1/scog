#!/usr/bin/env bash
#
# Container entrypoint for scog.
#
# scog reads its configuration directly from environment variables (see
# src/config.py), so this script only assembles the gunicorn invocation.
# Any additional arguments passed to the container are appended verbatim to
# gunicorn.
#
# Supported environment variables (consumed by the app):
#   ARTIFACT_DIR      Directory served/cached to (default: /artifacts).
#   PORT              Port to listen on (default: 5000).
#   UI_ENABLED        "true" to enable the HTML UI.
#   PUBLISH_ENABLED   "true" to enable the publish routes.
#   RELATIVE_URLS     "true" to emit relative URLs (behind an ingress).
#   MAX_PUBLISH       Max publish upload size in bytes (default: 20 MiB).
#   UPSTREAM_URL      Base URL of an upstream Galaxy v3 API for pull-through.
#   UPSTREAM_TOKEN    Bearer token sent to UPSTREAM_URL.
#   UPSTREAM_TIMEOUT  Per-request upstream read timeout in seconds (default: 30).
#   UPSTREAM_CONNECT_TIMEOUT
#                     Upstream connect timeout in seconds (default: 3); a down
#                     upstream fails fast and serving degrades to local content.
#   MERGE_UPSTREAM_VERSIONS
#                     "true" (default) unions upstream versions into the
#                     versions list so uncached pinned installs resolve;
#                     "false" for a strict local-only mirror.
#   SCOG_PUBLISH_API_KEYS
#                     Comma-separated list of API keys accepted for publish.
#
# Supported environment variables (consumed by this script / gunicorn):
#   GUNICORN_WORKERS  Number of worker processes (default: 2).
#   GUNICORN_THREADS  Threads per worker (default: 4).
#   GUNICORN_TIMEOUT  Worker timeout in seconds (default: 120).

set -euo pipefail

PORT="${PORT:-5000}"

exec gunicorn \
    --bind "0.0.0.0:${PORT}" \
    --workers "${GUNICORN_WORKERS:-2}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --access-logfile - \
    --error-logfile - \
    wsgi:app "$@"
