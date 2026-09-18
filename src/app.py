# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""Flask application: Ansible Galaxy v3 API mirror / pull-through cache.

Serves collections from a local artifacts directory, falling back to a
configured upstream Galaxy v3 API when content is missing locally, caching
whatever it fetches to disk. Optionally exposes publish routes (guarded by
an API key) and a small HTML UI.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import threading
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, request, send_file

from .auth import is_authorized
from .config import Config
from .models import ISO8601, Collection, CollectionInfo, parse_version
from .storage import Storage
from .upstream import UpstreamClient, UpstreamNotFound

log = logging.getLogger("scog")

NOT_FOUND = {"code": "not_found", "message": "Not found."}


def _candidate_index_paths(app_root: str) -> list[str]:
    return [
        os.path.join(os.getcwd(), "index.html"),
        os.path.join(app_root, "index.html"),
        os.path.join(os.path.dirname(app_root), "index.html"),
    ]


def _load_index_html(app_root: str) -> bytes:
    for path in _candidate_index_paths(app_root):
        try:
            with open(path, "rb") as fh:
                return fh.read()
        except OSError:
            continue
    return b"<!doctype html><title>scog</title><h1>scog</h1>"


class Scog:
    """Holds shared state and implements the request handlers."""

    def __init__(self, config: Config, storage: Storage, upstream: UpstreamClient | None):
        self.config = config
        self.storage = storage
        self.upstream = upstream
        self._publish_lock = threading.Lock()
        self._fetch_locks: dict[str, threading.Lock] = {}
        self._fetch_locks_guard = threading.Lock()

    # -- URL helpers ------------------------------------------------------
    def get_host(self) -> str:
        prefix = request.headers.get("X-Forwarded-Prefix", "")
        if self.config.relative:
            return prefix
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
        host = request.headers.get("X-Forwarded-Host", request.host)
        return f"{scheme}://{host}{prefix}"

    def collection_url(self, namespace: str, name: str) -> str:
        return f"{self.get_host()}/api/v3/collections/{namespace}/{name}/"

    def versions_url(self, namespace: str, name: str) -> str:
        return f"{self.get_host()}/api/v3/collections/{namespace}/{name}/versions/"

    def version_url(self, namespace: str, name: str, version: str) -> str:
        return f"{self.get_host()}/api/v3/collections/{namespace}/{name}/versions/{version}/"

    def artifact_url(self, filename: str) -> str:
        return f"{self.get_host()}/artifacts/{filename}"

    def import_task_url(self, task: str) -> str:
        return f"{self.get_host()}/api/v3/imports/collections/{task}/"

    # -- fetch coalescing -------------------------------------------------
    def _lock_fetch(self, key: str) -> threading.Lock:
        with self._fetch_locks_guard:
            lock = self._fetch_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._fetch_locks[key] = lock
        return lock

    # -- resolution -------------------------------------------------------
    def ensure_versions(self, namespace: str, name: str) -> list[Collection]:
        """Resolve the known versions of a collection, local content first.

        Local storage is the source of truth and is always served. Locally
        cached versions are included unconditionally and never require the
        upstream. This is version *discovery* for the ``/versions/`` list,
        which ansible-galaxy uses to resolve installs.

        When an upstream is configured and ``merge_upstream_versions`` is on
        (the default), upstream-published versions are unioned in as
        lightweight placeholders so that a pinned install of a version that
        isn't cached yet can be discovered and then pulled through on demand
        via :meth:`ensure_version`. The upstream call fails fast and degrades
        gracefully: if the upstream is unreachable, slow, or doesn't know the
        collection, we fall back to whatever is cached locally, so a broken
        or slow upstream never blocks or breaks serving of local content.

        When ``merge_upstream_versions`` is off, scog is strictly local-first:
        if anything is cached locally the upstream is not consulted, so only
        versions already on disk can be installed.
        """
        discovered = self.storage.read(namespace, name, "")
        if self.upstream is None:
            return discovered
        if discovered and not self.config.merge_upstream_versions:
            return discovered

        try:
            versions = self.upstream.list_versions(namespace, name)
        except UpstreamNotFound:
            return discovered
        except Exception as exc:  # noqa: BLE001
            log.warning("upstream: list versions for %s.%s: %s", namespace, name, exc)
            return discovered

        seen = {str(c.semver) for c in discovered}
        merged = list(discovered)
        for raw in versions:
            try:
                sv = parse_version(raw)
            except ValueError:
                continue
            if str(sv) in seen:
                continue
            seen.add(str(sv))
            merged.append(
                Collection(
                    collection_info=CollectionInfo(
                        namespace=namespace, name=name, version=str(sv)
                    )
                )
            )
        return merged

    def ensure_version(self, namespace: str, name: str, version: str) -> list[Collection]:
        """Resolve one version, pulling it through from upstream on miss."""
        coll = f"{namespace}.{name}:{version}"

        discovered = self.storage.read(namespace, name, version)
        if discovered:
            log.info("cache HIT: %s served from local storage", coll)
            return discovered
        if self.upstream is None:
            log.info("cache MISS: %s not in local storage and no upstream configured", coll)
            return discovered

        lock = self._lock_fetch(f"{namespace}.{name}.{version}")
        with lock:
            # Another request may have fetched it while we waited.
            discovered = self.storage.read(namespace, name, version)
            if discovered:
                log.info("cache HIT: %s served from local storage (concurrent fetch)", coll)
                return discovered

            log.info("cache MISS: %s not in local storage, fetching from upstream", coll)
            try:
                info = self.upstream.fetch_version(namespace, name, version)
            except UpstreamNotFound:
                log.info("cache MISS: %s not found upstream", coll)
                return []
            except Exception as exc:  # noqa: BLE001
                log.warning("upstream: fetch metadata for %s: %s", coll, exc)
                return []

            filename = info.filename or f"{namespace}-{name}-{version}.tar.gz"
            try:
                resp = self.upstream.download(info.download_url)
            except Exception as exc:  # noqa: BLE001
                log.warning("upstream: download artifact for %s: %s", coll, exc)
                return []
            try:
                self.storage.write_artifact(filename, resp.raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("upstream: store artifact for %s: %s", coll, exc)
                return []
            finally:
                resp.close()

            log.info("cache FILL: %s fetched from upstream and stored as %s", coll, filename)
            return self.storage.read(namespace, name, version)


# -- collection-set helpers ----------------------------------------------
def _sort_versions(versions: list[Collection]) -> list[Collection]:
    return sorted(versions, key=lambda c: c.semver, reverse=True)


def _prod_versions(versions: list[Collection]) -> list[Collection]:
    return [v for v in versions if not v.is_prerelease]


def _latest_version(all_sorted: list[Collection], prod_sorted: list[Collection]) -> str:
    if prod_sorted:
        return str(prod_sorted[0].semver)
    return str(all_sorted[0].semver)


def create_app(config: Config | None = None) -> Flask:
    config = config or Config.from_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = Flask(__name__)
    app.url_map.strict_slashes = False
    app.config["MAX_CONTENT_LENGTH"] = config.max_publish

    storage = Storage(config.artifacts)
    upstream = None
    if config.upstream_url:
        upstream = UpstreamClient(
            config.upstream_url,
            config.upstream_token,
            config.upstream_timeout,
            config.upstream_connect_timeout,
        )
    scog = Scog(config, storage, upstream)
    index_html = _load_index_html(app.root_path)

    app.extensions["scog"] = scog

    def not_found() -> Response:
        return jsonify(NOT_FOUND), 404

    # -- API root ---------------------------------------------------------
    @app.get("/api/")
    def api():
        return jsonify(
            {
                "available_versions": {"v3": "v3/"},
                "description": "scog Galaxy REST API",
            }
        )

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    # -- collection listing ----------------------------------------------
    @app.get("/api/v3/collections/")
    def collections():
        discovered = storage.read("", "", "")
        grouped: dict[str, list[Collection]] = {}
        for c in discovered:
            key = f"{c.collection_info.namespace}.{c.collection_info.name}"
            grouped.setdefault(key, []).append(c)

        results = [_build_collection_response(scog, versions) for versions in grouped.values()]
        return jsonify({"data": results})

    @app.get("/api/v3/collections/<namespace>/<name>/")
    def collection(namespace: str, name: str):
        discovered = scog.ensure_versions(namespace, name)
        if not discovered:
            return not_found()

        all_sorted = _sort_versions(discovered)
        prod_sorted = _sort_versions(_prod_versions(discovered))
        latest = _latest_version(all_sorted, prod_sorted)

        return jsonify(
            {
                "name": name,
                "namespace": namespace,
                "updated_at": all_sorted[0].created,
                "created_at": all_sorted[-1].created,
                "versions_url": scog.versions_url(namespace, name),
                "href": scog.collection_url(namespace, name),
                "highest_version": {
                    "href": scog.version_url(namespace, name, latest),
                    "version": latest,
                },
            }
        )

    @app.get("/api/v3/collections/<namespace>/<name>/versions/")
    def versions(namespace: str, name: str):
        discovered = scog.ensure_versions(namespace, name)
        if not discovered:
            return not_found()

        out = []
        for c in _sort_versions(discovered):
            v = str(c.semver)
            out.append({"href": scog.version_url(namespace, name, v), "version": v})
        return jsonify({"data": out})

    @app.get("/api/v3/collections/<namespace>/<name>/versions/<version>/")
    def version(namespace: str, name: str, version: str):
        discovered = scog.ensure_version(namespace, name, version)
        if not discovered:
            return not_found()
        c = discovered[0]

        signatures = [{"signature": sig} for sig in storage.read_signatures(c.path)]

        return jsonify(
            {
                "artifact": {"filename": c.filename, "sha256": c.sha},
                "collection": {
                    "name": name,
                    "href": scog.collection_url(namespace, name),
                },
                "name": name,
                "namespace": {"name": namespace},
                "download_url": scog.artifact_url(c.filename),
                "metadata": c.collection_info.to_dict(),
                "version": version,
                "signatures": signatures,
                "href": scog.version_url(namespace, name, version),
                "requires_ansible": c.requires_ansible,
            }
        )

    # -- artifacts --------------------------------------------------------
    @app.get("/artifacts/<path:filename>")
    def artifact(filename: str):
        filename = os.path.basename(filename)

        if storage.exists(filename):
            log.info("cache HIT: %s served from local storage", filename)
        else:
            parsed = _parse_artifact_filename(filename)
            if parsed and scog.upstream is not None:
                ns, nm, ver = parsed
                scog.ensure_version(ns, nm, ver)
            else:
                log.info(
                    "cache MISS: %s not in local storage and cannot be fetched from upstream",
                    filename,
                )

        path = storage.artifact_path(filename)
        if not os.path.exists(path):
            return not_found()
        return send_file(path, as_attachment=True, download_name=filename)

    # -- publish ----------------------------------------------------------
    if config.publish:

        @app.post("/api/v3/artifacts/collections/")
        def publish():
            if not is_authorized(request.headers.get("Authorization"), config.publish_api_keys):
                return jsonify({"code": "forbidden", "message": "Invalid or missing API key."}), 403

            with scog._publish_lock:
                sha256 = request.form.get("sha256", "")
                if not sha256:
                    return "publish error: missing sha256", 400

                file = request.files.get("file")
                if file is None:
                    return "publish error: missing file", 400

                try:
                    src = _prepare_form_file(file)
                except Exception as exc:  # noqa: BLE001
                    return f"publish error: {exc}", 400

                try:
                    dst = storage.write(sha256, file.filename or "", src)
                except Exception as exc:  # noqa: BLE001
                    return f"publish error: {exc}", 400

                return jsonify({"task": scog.import_task_url(dst)})

        @app.get("/api/v3/imports/collections/<task>/")
        def import_task(task: str):
            if not storage.exists(task):
                return not_found()
            finished = datetime.now(timezone.utc).strftime(ISO8601)
            return jsonify({"state": "completed", "finished_at": finished})

    # -- UI ---------------------------------------------------------------
    if config.ui:

        @app.get("/")
        @app.get("/index.html")
        def index():
            return Response(index_html, content_type="text/html; charset=utf-8")

        @app.get("/_ui/v1/docs/<namespace>/<name>/versions/<version>/")
        def docs(namespace: str, name: str, version: str):
            discovered = scog.ensure_version(namespace, name, version)
            if not discovered:
                return not_found()
            return jsonify({"readme": storage.read_readme(discovered[0].path)})

    return app


def _build_collection_response(scog: Scog, versions: list[Collection]) -> dict:
    all_sorted = _sort_versions(versions)
    prod_sorted = _sort_versions(_prod_versions(versions))
    namespace = all_sorted[0].collection_info.namespace
    name = all_sorted[0].collection_info.name
    latest = _latest_version(all_sorted, prod_sorted)

    return {
        "name": name,
        "namespace": namespace,
        "updated_at": all_sorted[0].created,
        "created_at": all_sorted[-1].created,
        "versions_url": scog.versions_url(namespace, name),
        "highest_version": {
            "href": scog.version_url(namespace, name, latest),
            "version": latest,
        },
    }


def _parse_artifact_filename(filename: str) -> tuple[str, str, str] | None:
    """Split ``<namespace>-<name>-<version>.tar.gz`` into its parts.

    Namespace and name never contain hyphens, so the first two hyphen tokens
    are namespace and name and the remainder (which may contain hyphens, e.g.
    a ``1.0.0-beta1`` prerelease) is the version.
    """
    if not filename.endswith(".tar.gz"):
        return None
    stem = filename[: -len(".tar.gz")]
    parts = stem.split("-", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def _prepare_form_file(file) -> io.BytesIO:
    """Return a seekable BytesIO of the uploaded file, honoring base64 CTE."""
    cte = (file.headers.get("Content-Transfer-Encoding") or "").strip().lower()
    raw = file.stream.read()
    if cte == "base64":
        raw = base64.b64decode(raw)
    return io.BytesIO(raw)
