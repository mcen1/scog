# AGENTS.md

Guidance for AI coding agents (and humans) working in the `scog` project.

## What this project is

`scog` is a Python/Flask reimplementation of [`amanda`](../amanda) (a Go
project by Matt Martz). It mimics the **Ansible Galaxy v3 REST API** so it can
act as a **local mirror and pull-through cache** for collections, sitting in
front of an upstream Galaxy API such as `galaxy.ansible.com`.

Core behavior, in priority order:

1. **Local content is authoritative.** For any request, on-disk artifacts in
   the artifacts directory are checked first and served if present. A slow or
   broken upstream must never block or degrade serving of content we already
   have. This is the whole point: when `galaxy.ansible.com` is having problems,
   we keep working from local content.
2. **Pull-through on miss.** Only when content is *not* found locally (and an
   upstream is configured) do we reach out to the upstream, download the
   artifact, store it to disk, and serve it. Subsequent requests are local.
3. **Publish.** Optionally, users can publish collections via
   `ansible-galaxy collection publish`, guarded by an API key.

There is **no database**. The artifacts directory (`*.tar.gz` files, plus
optional `*.asc` detached signatures) is the sole source of truth.

## Layout

```
scog/
├── src/                 # the Python package (imported as `src`)
│   ├── __init__.py      # exposes create_app, Config
│   ├── app.py           # Flask app factory + all routes + scog handler class
│   ├── config.py        # Config dataclass, all env-var parsing
│   ├── models.py        # Collection model, tar/MANIFEST parsing, validation
│   ├── storage.py       # on-disk scanning, caching, read/write artifacts
│   ├── upstream.py      # Galaxy v3 pull-through HTTP client
│   └── auth.py          # publish API-key extraction/validation
├── wsgi.py              # gunicorn entrypoint: `gunicorn wsgi:app`
├── index.html           # optional HTML UI (served when UI_ENABLED=true)
├── requirements.txt
├── Dockerfile           # UBI10-based image
├── entrypoint.sh        # container entrypoint (assembles gunicorn command)
├── runlocal.sh          # local dev: venv + install + run
└── buildlocal.sh        # `docker build` helper
```

> NOTE: The importable package directory is `src/`. `wsgi.py` does
> `from src import create_app`. If you rename `src/`, update `wsgi.py`, the
> `Dockerfile` `COPY src/ ...` line, and any `FLASK_APP` references.

## Configuration (environment variables)

All configuration is via env vars, parsed in `src/config.py`
(`Config.from_env`). There are no CLI flags for the app itself.

| Env var                 | Default                       | Purpose |
|-------------------------|-------------------------------|---------|
| `ARTIFACT_DIR`          | `artifacts`                   | Directory served/cached to |
| `PORT`                  | `5000`                        | Listen port |
| `UI_ENABLED`            | `false`                       | Serve the HTML UI + docs endpoint |
| `PUBLISH_ENABLED`       | `false`                       | Enable publish + import-task routes |
| `RELATIVE_URLS`         | `false`                       | Emit path-only URLs (behind ingress) |
| `MAX_PUBLISH`           | `20971520` (20 MiB)           | Max publish upload size in bytes |
| `UPSTREAM_URL`          | `""` (disabled)               | Base URL of upstream Galaxy v3 API |
| `UPSTREAM_TOKEN`        | `""`                          | Bearer token for the upstream |
| `UPSTREAM_TIMEOUT`      | `30`                          | Per-request upstream read timeout (seconds) |
| `UPSTREAM_CONNECT_TIMEOUT` | `3`                        | Upstream connect timeout (seconds); fast-fail to local when upstream is down |
| `MERGE_UPSTREAM_VERSIONS` | `true`                      | Union upstream versions into the versions list so uncached pinned installs resolve; `false` = strict local-only |
| `SCOG_PUBLISH_API_KEYS` | `""`                          | Comma-separated list of valid publish keys |

Boolean vars are truthy for `1/true/yes/on` (case-insensitive).

## Routes (mirror of the Galaxy v3 API)

Always available:

- `GET /api/` — API root/version discovery
- `GET /api/v3/collections/` — list locally-cached collections (local only)
- `GET /api/v3/collections/<ns>/<name>/` — collection detail
- `GET /api/v3/collections/<ns>/<name>/versions/` — versions list
- `GET /api/v3/collections/<ns>/<name>/versions/<version>/` — version detail
- `GET /artifacts/<filename>` — download an artifact (pull-through capable)
- `GET /healthz` — health check (scog addition, not in amanda)

Only when `PUBLISH_ENABLED=true`:

- `POST /api/v3/artifacts/collections/` — publish (requires API key)
- `GET /api/v3/imports/collections/<task>/` — import task status

Only when `UI_ENABLED=true`:

- `GET /` and `GET /index.html` — HTML UI
- `GET /_ui/v1/docs/<ns>/<name>/versions/<version>/` — README extraction

## Key behavioral invariants (do not regress these)

- **Serving cached content never contacts the upstream.** A specific version's
  detail (`ensure_version`) and its artifact download are strictly local-first:
  if the version is on disk it is served immediately, with no upstream call.
  This is the whole point -- when `galaxy.ansible.com` is down or slow, already
  cached content keeps working.
- **`ensure_versions` merges local + upstream for *discovery*, but degrades
  gracefully.** The `/versions/` list is what ansible-galaxy uses to resolve an
  install, so to let a pinned install of a *not-yet-cached* version resolve, the
  list must include upstream-published versions (added as lightweight
  placeholders; no artifact is downloaded here). Local versions are always
  included and authoritative. The upstream call fails fast (`UPSTREAM_CONNECT_TIMEOUT`)
  and falls back to local on any error/timeout, so a broken or slow upstream
  never breaks or blocks the list. Set `MERGE_UPSTREAM_VERSIONS=false` for a
  strict local-only mirror (only versions already on disk are installable).
- **`ensure_version` (single pinned version) is local-first**, with a per-key
  lock so concurrent requests for the same uncached version coalesce into a
  single upstream download.
- **`/artifacts/<filename>` is self-healing.** ansible-galaxy caches
  `download_url`s client-side and may request an artifact directly without
  first hitting version metadata. The artifact route parses
  `<ns>-<name>-<version>.tar.gz` and pulls through on miss.
- **Path traversal is prevented** by reducing artifact filenames to their
  basename (`Storage.artifact_path`).
- **Publish fails closed.** If `SCOG_PUBLISH_API_KEYS` is empty, nobody can
  publish (403). A valid key must appear in that list. ansible-galaxy sends
  `Authorization: Token <key>`; `Bearer <key>` and a bare key are also
  accepted (see `src/auth.py`).
- **Upstream artifact downloads send `Accept: application/json`**, not
  `application/octet-stream` — the galaxy CDN returns HTTP 406 for the latter.

## How ansible-galaxy resolves an install (useful when debugging)

For `ansible-galaxy collection install -s http://host:5000/ ns.name:version`:

1. `GET /api` (discovery)
2. `GET /api/v3/collections/ns/name/`
3. `GET /api/v3/collections/ns/name/versions/?limit=100` (must include the
   requested version, or resolution fails with "Could not satisfy
   requirements")
4. `GET /api/v3/collections/ns/name/versions/<version>/`
5. `GET /artifacts/ns-name-version.tar.gz`

A "Could not satisfy requirements" error with no HTTP error usually means the
versions list (step 3) did not contain the requested version — i.e. it isn't
local and the upstream is unset/unreachable/doesn't have it.

## Running locally

```bash
./runlocal.sh                                   # cache in front of galaxy.ansible.com (default)
UPSTREAM_URL="" ./runlocal.sh                   # local-only mirror, no upstream
SERVER=flask ./runlocal.sh                      # Flask dev server + reloader
PORT=8080 PUBLISH_ENABLED=false ./runlocal.sh
```

`runlocal.sh` creates `.venv`, installs `requirements.txt`, and runs gunicorn
with `--reload`. Its defaults set `SCOG_PUBLISH_API_KEYS=localdevkey` and
`PUBLISH_ENABLED=true` for convenience — do not copy those defaults into
production.

Then, in another shell:

```bash
ansible-galaxy collection install -s http://localhost:5000/ community.general
```

## Building the container

```bash
./buildlocal.sh                 # docker build -> localhost/scog:latest
```

The image is UBI10-based, runs as the non-root `notroot` user, and serves via
gunicorn. `ARTIFACT_DIR=/artifacts` is a
`VOLUME`. Configure at runtime with the env vars above.

## Conventions for changes

- Keep the env-var-driven configuration model; add new knobs to
  `Config.from_env` and document them here and in `entrypoint.sh`.
- Preserve the "local content first" priority. Serving a *cached* version
  (version detail + artifact) must never depend on the upstream. The
  `/versions/` list may consult the upstream for discovery, but must always
  include local versions and degrade to local on any upstream error.
- The response JSON shapes are consumed by `ansible-galaxy`; match the Galaxy
  v3 API. When in doubt, compare against the original Go implementation in
  `../amanda/handlers/handlers.go`.
- Comment only where intent is non-obvious; the invariants above are the
  things worth explaining.

## Verifying changes

There is no formal test suite yet. At minimum, before considering a change
done, run an end-to-end check with a real `ansible-galaxy`:

- a local-only install (content already in `ARTIFACT_DIR`, `UPSTREAM_URL`
  unset) — must succeed without any network calls; and
- a pull-through install (empty `ARTIFACT_DIR`, `UPSTREAM_URL` pointing at a
  reachable Galaxy v3 API, e.g. another `scog`/`amanda` instance) — must fetch,
  cache to disk, and install.
