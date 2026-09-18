# SCOG - Starless Cloud of Gas

A small Flask web application that mimics the Ansible Galaxy v3 API for
collections, backed by nothing more than a directory of artifacts. It serves
**local content first** and, when configured, transparently **pulls through**
from an upstream Galaxy API (e.g. `galaxy.ansible.com`) for anything not yet
cached — storing it to disk so it is served locally from then on.

It is a Python port of [`amanda`](https://github.com/sivel/amanda) (originally by Matt Martz), created
so the codebase is approachable to a Python-first team.

## Why

When `galaxy.ansible.com` is slow or down, installs should not break. `scog`
trusts local content first and foremost: if a collection version is already on
disk it is served immediately, and the upstream is never consulted on that path.

To let you install a version that *isn't* cached yet (e.g. pinning
`ns.name==1.2.3` before it's on disk), scog discovers available versions by
unioning your local set with the upstream's published versions in the versions
list. That upstream lookup fails fast and falls back to local content if the
upstream is unreachable, so a broken or slow upstream never blocks serving of
what you already have. Set `MERGE_UPSTREAM_VERSIONS=false` to disable this and
run a strict local-only mirror (only versions already on disk are installable).

## Quick start (local)

```bash
./runlocal.sh
# in another shell:
ansible-galaxy collection install -s http://localhost:5000/ community.general
```

By default `runlocal.sh` runs as a pull-through cache in front of
`galaxy.ansible.com`. To run as a purely local mirror (only what's in
`artifacts/`), start it with `UPSTREAM_URL="" ./runlocal.sh`.

Drop `ansible-galaxy collection build`/`download` artifacts
(`namespace-name-version.tar.gz`) into the artifacts directory to serve them
locally.

## Configuration

All configuration is via environment variables (see `AGENTS.md` for the full
table). The most common:

| Env var                 | Default     | Purpose |
|-------------------------|-------------|---------|
| `ARTIFACT_DIR`          | `artifacts` | Directory served/cached to |
| `PORT`                  | `5000`      | Listen port |
| `UI_ENABLED`            | `false`     | Serve the HTML UI |
| `PUBLISH_ENABLED`       | `false`     | Enable publishing |
| `UPSTREAM_URL`          | unset       | Upstream Galaxy v3 API for pull-through |
| `UPSTREAM_TOKEN`        | unset       | Bearer token for the upstream |
| `MERGE_UPSTREAM_VERSIONS` | `true`    | Union upstream versions for discovery (`false` = local-only) |
| `SCOG_PUBLISH_API_KEYS` | unset       | Comma-separated valid publish API keys |

## Publishing

When `PUBLISH_ENABLED=true`, users can publish with:

```bash
ansible-galaxy collection publish -s http://localhost:5000/ \
    --api-key "$YOUR_KEY" namespace-name-1.0.0.tar.gz
```

Publishing requires an API key. Set `SCOG_PUBLISH_API_KEYS` to a
comma-separated list of accepted keys; a request whose key is not in that list
(or which supplies no key) is rejected with **403**. If the variable is unset
or empty, nobody can publish (fail closed).

The auth check lives in `src/auth.py` and is intentionally simple. Because the
source is available, you can replace it with your own scheme (OIDC, LDAP, a
reverse proxy, etc.) to suit your needs.

## Container

```bash
./buildlocal.sh     # builds localhost/scog:latest
docker run --rm -p 5000:5000 -v /path/to/artifacts:/artifacts \
    -e UPSTREAM_URL=https://galaxy.ansible.com localhost/scog:latest
```

## Reverse proxy

Behind a path prefix, send `X-Forwarded-Prefix` (and optionally
`X-Forwarded-Proto` / `X-Forwarded-Host`) so generated URLs are correct, or set
`RELATIVE_URLS=true` to emit path-only URLs.

## Signatures

Alongside a `namespace-name-version.tar.gz` artifact, place a detached ASCII
GPG signature named `namespace-name-version.asc`; it is exposed in the version
metadata.

## License

Derived from `amanda`, which is GPL-3.0+. See the original project for the full
license text.
