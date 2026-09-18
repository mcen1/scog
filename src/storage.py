# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""On-disk artifact storage.

The artifacts directory is the sole source of truth: it holds the
``*.tar.gz`` collection artifacts (and optional ``*.asc`` signatures). This
module scans and parses them into :class:`~scog.models.Collection` objects,
caches the parsed metadata in memory keyed by (filename, mtime), and handles
writing newly published or upstream-fetched artifacts to disk.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import BinaryIO, Iterable

from . import models
from .models import Collection, collection_from_tar

log = logging.getLogger("scog.storage")


def _format_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(models.ISO8601)


class Storage:
    def __init__(self, artifacts: str) -> None:
        self.artifacts = artifacts
        self._cache: dict[tuple[str, str], Collection] = {}
        self._sig_cache: dict[tuple[str, str], list[str]] = {}
        self._readme_cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        self._logged_errors: set[str] = set()

    # -- error logging ----------------------------------------------------
    def _log_err_once(self, key: str, msg: str) -> None:
        with self._lock:
            if key in self._logged_errors:
                return
            self._logged_errors.add(key)
        log.error("error: %s", msg)

    # -- scanning / reading ----------------------------------------------
    def read(self, namespace: str, name: str, version: str) -> list[Collection]:
        """Return collections in the artifacts dir matching the filters.

        Empty namespace and name means "all". A non-empty version short
        circuits once a match is found.
        """
        try:
            entries = os.listdir(self.artifacts)
        except FileNotFoundError:
            return []

        collections: list[Collection] = []
        for filename in entries:
            if not filename.endswith(".tar.gz"):
                continue
            full = os.path.join(self.artifacts, filename)
            if not os.path.isfile(full):
                continue
            collection = self._process_file(filename)
            if collection is None:
                continue
            if collection.matches(namespace, name, version):
                collections.append(collection)
                if version != "":
                    break
        return collections

    def _process_file(self, filename: str) -> Collection | None:
        path = os.path.join(self.artifacts, filename)
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return None

        key = (filename, _format_mtime(mtime))
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached

        try:
            collection = collection_from_tar(path, None)
        except Exception as exc:  # noqa: BLE001 - parity with LogErrOnce
            self._log_err_once(f"{path}:{exc}", f"{path} {exc}")
            return None

        collection.filename = filename
        collection.path = path
        collection.created = _format_mtime(mtime)

        with self._lock:
            self._cache[key] = collection
        return collection

    def read_signatures(self, path: str) -> list[str]:
        """Read the detached ``.asc`` signature next to an artifact, if any."""
        if not path.endswith(".tar.gz"):
            stem = os.path.splitext(path)[0]
        else:
            stem = path[: -len(".tar.gz")]
        signature_path = stem + ".asc"

        try:
            mtime = os.stat(signature_path).st_mtime
        except OSError:
            return []

        key = (signature_path, _format_mtime(mtime))
        with self._lock:
            cached = self._sig_cache.get(key)
        if cached is not None:
            return cached

        try:
            with open(signature_path, "rb") as fh:
                signature = fh.read().strip().decode("utf-8", "replace")
        except OSError:
            return []

        signatures = [signature]
        with self._lock:
            self._sig_cache[key] = signatures
        return signatures

    def read_readme(self, path: str) -> str:
        """Extract README.md (case-insensitive) from an artifact tarball."""
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            return ""

        key = (path, _format_mtime(mtime))
        with self._lock:
            cached = self._readme_cache.get(key)
        if cached is not None:
            return cached

        try:
            with open(path, "rb") as fh:
                files = models.load_files_from_tar(fh, True, "readme.md")
        except OSError:
            return ""

        data = files.get("readme.md")
        if data is None:
            return ""
        readme = data.decode("utf-8", "replace")
        with self._lock:
            self._readme_cache[key] = readme
        return readme

    # -- writing ----------------------------------------------------------
    def write(self, sha256: str, filename: str, src: BinaryIO) -> str:
        """Validate and persist a published artifact; return its stored name."""
        collection = collection_from_tar(filename, src)
        models.validate_collection(collection, sha256)

        ci = collection.collection_info
        dst_name = f"{ci.namespace}-{ci.name}-{ci.version}.tar.gz"
        dst_path = os.path.join(self.artifacts, dst_name)

        if self.exists(dst_name):
            raise ValueError("collection version already exists")

        src.seek(0)
        os.makedirs(self.artifacts, exist_ok=True)
        with open(dst_path, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        return dst_name

    def write_artifact(self, filename: str, reader: Iterable[bytes] | BinaryIO) -> str:
        """Atomically write an upstream-fetched artifact to the artifacts dir.

        No-op returning the existing path if the file already exists, so this
        is safe to call opportunistically.
        """
        dst_path = os.path.join(self.artifacts, filename)
        if self.exists(filename):
            return dst_path

        os.makedirs(self.artifacts, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".scog-fetch-", suffix=".tmp", dir=self.artifacts)
        try:
            with os.fdopen(fd, "wb") as tmp:
                if hasattr(reader, "read"):
                    while True:
                        chunk = reader.read(1 << 20)  # type: ignore[union-attr]
                        if not chunk:
                            break
                        tmp.write(chunk)
                else:
                    for chunk in reader:  # type: ignore[assignment]
                        tmp.write(chunk)
            os.replace(tmp_path, dst_path)
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return dst_path

    # -- helpers ----------------------------------------------------------
    def exists(self, filename: str) -> bool:
        return os.path.exists(os.path.join(self.artifacts, filename))

    def artifact_path(self, filename: str) -> str:
        """Absolute path for filename, reduced to its basename to prevent
        path traversal out of the artifacts directory."""
        return os.path.join(self.artifacts, os.path.basename(filename))
