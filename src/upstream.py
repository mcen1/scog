# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""Upstream pull-through client.

When an upstream base URL is configured, collections or versions not found
in local storage are fetched from an upstream Ansible Galaxy v3 compatible
API (e.g. galaxy.ansible.com, Automation Hub, or another scog/amanda
instance), stored to disk, and served locally from then on.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import requests
import urllib3
import os


class UpstreamNotFound(Exception):
    """Raised when the upstream does not have the requested resource."""


@dataclass
class VersionInfo:
    version: str
    filename: str
    sha256: str
    download_url: str


class UpstreamClient:
    """A Fetcher speaking the standard Ansible Galaxy v3 collections API."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 30.0,
        connect_timeout: float = 3.05,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        # (connect, read) timeout tuple: a down/unreachable upstream fails on
        # connect within connect_timeout so callers degrade to local content
        # quickly, while slow-but-alive responses are still allowed up to the
        # full read timeout.
        self._timeout = (connect_timeout, timeout)
        self._session = requests.Session()
        self._session.verify = os.environ.get("REQUESTS_CA_BUNDLE") or False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    def _headers(self, accept: str = "application/json") -> dict[str, str]:
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def list_versions(self, namespace: str, name: str) -> list[str]:
        """Return every published version, walking all pagination pages.

        Raises :class:`UpstreamNotFound` when the collection is unknown.
        """
        next_url = (
            f"{self.base_url}/api/v3/collections/{namespace}/{name}/versions/"
        )
        versions: list[str] = []
        first_page = True

        while next_url:
            resp = self._session.get(
                next_url, headers=self._headers(), timeout=self._timeout
            )
            if resp.status_code == 404 and first_page:
                raise UpstreamNotFound(f"{namespace}.{name}")
            if resp.status_code != 200:
                raise RuntimeError(
                    f"upstream: unexpected status {resp.status_code} fetching {next_url}"
                )

            payload = resp.json()
            for item in payload.get("data") or []:
                version = item.get("version")
                if version:
                    versions.append(version)

            raw_next = ((payload.get("links") or {}).get("next")) or ""
            next_url = urljoin(resp.url, raw_next) if raw_next else ""
            first_page = False

        return versions

    def fetch_version(
        self, namespace: str, name: str, version: str
    ) -> VersionInfo:
        """Return metadata (including download URL) for a specific version."""
        url = (
            f"{self.base_url}/api/v3/collections/{namespace}/{name}/versions/{version}/"
        )
        resp = self._session.get(
            url, headers=self._headers(), timeout=self._timeout
        )
        if resp.status_code == 404:
            raise UpstreamNotFound(f"{namespace}.{name}:{version}")
        if resp.status_code != 200:
            raise RuntimeError(
                f"upstream: unexpected status {resp.status_code} fetching {url}"
            )

        payload = resp.json()
        artifact = payload.get("artifact") or {}
        download_url = payload.get("download_url") or ""
        if not download_url:
            raise RuntimeError(f"upstream: no download_url in response from {url}")

        return VersionInfo(
            version=payload.get("version", "") or "",
            filename=artifact.get("filename", "") or "",
            sha256=artifact.get("sha256", "") or "",
            download_url=download_url,
        )

    def download(self, url: str) -> requests.Response:
        """Stream the artifact contents for a resolved download URL.

        Returns a streaming Response; the caller reads ``.raw`` / iterates.
        """
        resp = self._session.get(
            url,
            headers=self._headers(),
            timeout=self._timeout,
            stream=True,
        )
        if resp.status_code != 200:
            resp.close()
            raise RuntimeError(
                f"upstream: unexpected status {resp.status_code} downloading {url}"
            )
        resp.raw.decode_content = True
        return resp
