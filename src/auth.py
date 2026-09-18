# Python/Flask port of amanda (originally (c) Matt Martz, GPL-3.0+)
"""Publish authentication.

Publishing requires an API key, supplied the way ansible-galaxy sends it:
an ``Authorization`` header. Both the ``Token <key>`` scheme (used by
``ansible-galaxy`` against a Galaxy v3 API) and ``Bearer <key>`` are
accepted, as is a bare key. The supplied key must be present in the set of
keys configured via the ``SCOG_PUBLISH_API_KEYS`` environment variable.

This is intentionally simple: the source is available, so operators can swap
this out for their own auth scheme (LDAP, OIDC, a reverse proxy, etc.).
"""

from __future__ import annotations


def extract_api_key(authorization_header: str | None) -> str:
    """Pull the bare key out of an Authorization header value."""
    if not authorization_header:
        return ""
    value = authorization_header.strip()
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() in {"token", "bearer"}:
        return parts[1].strip()
    return value


def is_authorized(authorization_header: str | None, valid_keys: set[str]) -> bool:
    """Return True if the request presents a configured, valid API key.

    When no keys are configured, no one is authorized (fail closed): the
    operator must set ``SCOG_PUBLISH_API_KEYS`` to enable publishing.
    """
    if not valid_keys:
        return False
    key = extract_api_key(authorization_header)
    if not key:
        return False
    return key in valid_keys
