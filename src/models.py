# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""Collection domain model.

A "collection" is an Ansible Galaxy collection artifact: a gzipped tarball
named ``<namespace>-<name>-<version>.tar.gz`` containing a ``MANIFEST.json``
(and optionally ``meta/runtime.yml``). This module knows how to parse those
tarballs into :class:`Collection` instances and how to validate an uploaded
artifact before it is stored.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import semantic_version
import yaml

# Format used for the ``created`` timestamp, matching the original Go
# implementation: microsecond precision with a numeric UTC offset.
ISO8601 = "%Y-%m-%dT%H:%M:%S.%f%z"


def parse_version(raw: str) -> semantic_version.Version:
    """Parse a semver string, coercing loose input where possible."""
    return semantic_version.Version.coerce(raw)


@dataclass
class CollectionInfo:
    namespace: str = ""
    name: str = ""
    version: str = ""
    dependencies: dict[str, Any] = field(default_factory=dict)
    repository: str = ""
    documentation: str = ""
    homepage: str = ""
    issues: str = ""
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_manifest(cls, data: dict[str, Any]) -> "CollectionInfo":
        return cls(
            namespace=data.get("namespace", "") or "",
            name=data.get("name", "") or "",
            version=str(data.get("version", "") or ""),
            dependencies=data.get("dependencies") or {},
            repository=data.get("repository", "") or "",
            documentation=data.get("documentation", "") or "",
            homepage=data.get("homepage", "") or "",
            issues=data.get("issues", "") or "",
            tags=data.get("tags") or [],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "version": self.version,
            "dependencies": self.dependencies,
            "repository": self.repository,
            "documentation": self.documentation,
            "homepage": self.homepage,
            "issues": self.issues,
            "tags": self.tags,
        }


@dataclass
class Collection:
    collection_info: CollectionInfo
    filename: str = ""
    path: str = ""
    sha: str = ""
    created: str = ""
    requires_ansible: str = ""

    # Cached parsed semver; computed lazily via ``semver``.
    _semver: semantic_version.Version | None = field(
        default=None, repr=False, compare=False
    )

    @property
    def semver(self) -> semantic_version.Version:
        if self._semver is None:
            self._semver = parse_version(self.collection_info.version)
        return self._semver

    @property
    def version(self) -> str:
        return self.collection_info.version

    @property
    def is_prerelease(self) -> bool:
        return bool(self.semver.prerelease)

    def matches(self, namespace: str, name: str, version: str) -> bool:
        """Mirror amanda's Collection.Matches filtering semantics."""
        if namespace == "" and name == "":
            return True
        if (
            self.collection_info.namespace != namespace
            or self.collection_info.name != name
        ):
            return False
        if version == "":
            return True
        return self.collection_info.version == version


def validate_name(s: str) -> bool:
    """Namespace/name must match ``[a-z][a-z0-9_]+`` and be >= 2 chars."""
    if len(s) < 2:
        return False
    if not ("a" <= s[0] <= "z"):
        return False
    for ch in s[1:]:
        if not (("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_"):
            return False
    return True


def validate_collection(collection: Collection, expected_sha256: str) -> None:
    """Raise ValueError if the collection is malformed or the sha mismatches."""
    if collection.sha.lower() != expected_sha256.lower():
        raise ValueError("checksum mismatch")
    ci = collection.collection_info
    if not validate_name(ci.namespace):
        raise ValueError("invalid namespace")
    if not validate_name(ci.name):
        raise ValueError("invalid name")


def _sha256_of(fileobj: BinaryIO) -> str:
    fileobj.seek(0)
    h = hashlib.sha256()
    for chunk in iter(lambda: fileobj.read(1 << 20), b""):
        h.update(chunk)
    fileobj.seek(0)
    return h.hexdigest()


def load_files_from_tar(
    fileobj: BinaryIO,
    case_insensitive: bool,
    *filenames: str,
) -> dict[str, bytes]:
    """Extract a specific set of member files from a gzipped tarball.

    When ``case_insensitive`` is true the caller must pass lowercase names,
    and member names are lowercased before matching (used for ``README.md``).
    Reading stops early once every requested file has been found.
    """
    fileobj.seek(0)
    wanted = set(filenames)
    result: dict[str, bytes] = {}

    with gzip.GzipFile(fileobj=fileobj) as gz:
        with tarfile.open(fileobj=gz, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                name = member.name.lower() if case_insensitive else member.name
                if name in wanted:
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    result[name] = extracted.read()
                    if len(result) == len(wanted):
                        break
    fileobj.seek(0)
    return result


def collection_from_tar(path: str, fileobj: BinaryIO | None = None) -> Collection:
    """Build a Collection by parsing MANIFEST.json (and meta/runtime.yml).

    If ``fileobj`` is None the tarball is opened from ``path``; otherwise the
    provided seekable binary stream is parsed (used for uploads).
    """
    own = False
    if fileobj is None:
        fileobj = open(path, "rb")
        own = True
    try:
        files = load_files_from_tar(
            fileobj, False, "MANIFEST.json", "meta/runtime.yml"
        )
        manifest_data = files.get("MANIFEST.json")
        if manifest_data is None:
            raise ValueError(f"MANIFEST.json not found in {path}")

        manifest = json.loads(manifest_data)
        info = CollectionInfo.from_manifest(manifest.get("collection_info", {}))
        collection = Collection(
            collection_info=info,
            created=str(manifest.get("created", "") or ""),
        )

        runtime_data = files.get("meta/runtime.yml")
        if runtime_data is not None:
            try:
                runtime = yaml.safe_load(runtime_data) or {}
                collection.requires_ansible = str(
                    runtime.get("requires_ansible", "") or ""
                )
            except yaml.YAMLError:
                pass

        collection.sha = _sha256_of(fileobj)
        return collection
    finally:
        if own:
            fileobj.close()
