# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""Runtime configuration for scog, sourced from environment variables.

The container image is configured declaratively via environment variables
(see ``entrypoint.sh``), mirroring the flags of the original Go project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _parse_keys(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {k.strip() for k in raw.split(",") if k.strip()}


@dataclass
class Config:
    """Application configuration.

    Every field has a sane default so the app boots with zero configuration
    and behaves as a read-only local mirror of ``artifacts/``.
    """

    artifacts: str = "artifacts"
    relative: bool = False
    ui: bool = False
    publish: bool = False
    # Max upload size for publish, in bytes (default 20 MiB, matching amanda).
    max_publish: int = 20 << 20

    upstream_url: str = ""
    upstream_token: str = ""
    # Timeout, in seconds, for each request made to the upstream.
    upstream_timeout: float = 30.0
    # Connect timeout, in seconds, applied to upstream requests so a
    # down/unreachable upstream fails fast and we degrade to local content
    # instead of hanging for the full upstream_timeout.
    upstream_connect_timeout: float = 3.05

    # When True, the versions-list endpoint unions locally-cached versions
    # with those published upstream, so a pinned install of a version that
    # isn't cached yet can still be discovered and pulled through. Local
    # content is always authoritative and served without contacting the
    # upstream (version detail and artifact download are pure local-first);
    # if the upstream is unreachable the list degrades gracefully to
    # local-only. When False, scog is strictly local-first: if any version
    # of a collection is cached locally the upstream is never consulted for
    # it, so you can only install versions already on disk. No effect when
    # no upstream is configured.
    merge_upstream_versions: bool = True

    # Set of API keys accepted for publishing.
    publish_api_keys: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "Config":
        artifacts = os.path.abspath(os.getenv("ARTIFACT_DIR", "artifacts"))
        return cls(
            artifacts=artifacts,
            relative=_env_bool("RELATIVE_URLS"),
            ui=_env_bool("UI_ENABLED"),
            publish=_env_bool("PUBLISH_ENABLED"),
            max_publish=_env_int("MAX_PUBLISH", 20 << 20),
            upstream_url=os.getenv("UPSTREAM_URL", "").rstrip("/"),
            upstream_token=os.getenv("UPSTREAM_TOKEN", ""),
            upstream_timeout=float(_env_int("UPSTREAM_TIMEOUT", 30)),
            upstream_connect_timeout=float(_env_int("UPSTREAM_CONNECT_TIMEOUT", 3)),
            merge_upstream_versions=_env_bool("MERGE_UPSTREAM_VERSIONS", True),
            publish_api_keys=_parse_keys(os.getenv("SCOG_PUBLISH_API_KEYS")),
        )
