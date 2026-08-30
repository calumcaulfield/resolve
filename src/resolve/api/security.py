"""API authentication and webhook signing.

Two things the audited original got wrong, fixed here:

* Token comparison uses `secrets.compare_digest`. The original compared a
  shared token with `===`, which leaks length and prefix information through
  timing. It is a small hole, but it is free to close.
* Outbound webhooks are HMAC-signed with a timestamp, and the receiver is
  expected to reject signatures outside a replay window. The original sent
  unsigned JSON, so a receiver had no way to know a callback was genuine.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Header, HTTPException, status

from resolve.config import get_settings

SIGNATURE_HEADER = "x-resolve-signature"
TIMESTAMP_HEADER = "x-resolve-timestamp"
REPLAY_WINDOW_SECONDS = 300


async def require_api_key(x_api_key: str = Header(default="")) -> str:
    settings = get_settings()
    for candidate in settings.security.api_keys:
        if secrets.compare_digest(x_api_key, candidate):
            return x_api_key
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def sign_payload(
    payload: dict[str, Any], secret: str, timestamp: int | None = None
) -> tuple[str, str]:
    """Return `(timestamp, signature)` for an outbound webhook body."""
    ts = str(timestamp or int(time.time()))
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    digest = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    return ts, f"sha256={digest}"


def verify_signature(payload: dict[str, Any], secret: str, timestamp: str, signature: str) -> bool:
    try:
        age = abs(int(time.time()) - int(timestamp))
    except ValueError:
        return False
    if age > REPLAY_WINDOW_SECONDS:
        return False
    _, expected = sign_payload(payload, secret, int(timestamp))
    return hmac.compare_digest(expected, signature)
