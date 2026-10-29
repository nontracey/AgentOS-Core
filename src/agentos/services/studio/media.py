"""Short-lived signed media links so <img>/<video> can stream studio media with Range support.

A link names one asset, its owner and an expiry; the session token never enters a URL. A link
may ask for a derivative of the same asset (thumbnail width, browser preview, poster frame),
which needs no further authorization than the asset itself.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from urllib.parse import quote

LINK_TTL = 6 * 3600
VARIANTS = {"", "preview", "poster"}


class MediaSigner:
    def __init__(self, key_file: Path) -> None:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        if not key_file.is_file():
            key_file.write_bytes(secrets.token_bytes(32))
            os.chmod(key_file, 0o600)
        self._key = key_file.read_bytes()

    def _digest(self, asset_id: str, tenant: str, subject: str, expires: int) -> str:
        message = f"{asset_id}\0{tenant}\0{subject}\0{expires}".encode()
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()[:40]

    def url(self, asset_id: str, tenant: str, subject: str, *, width: int = 0, variant: str = "",
            ttl: int = LINK_TTL) -> str:
        # Round expiry to the hour so repeated listings return identical, cacheable URLs.
        expires = (int(time.time()) // 3600 + 1) * 3600 + ttl
        query = f"e={expires}&t={quote(tenant)}&sig={self._digest(asset_id, tenant, subject, expires)}"
        if width:
            query += f"&w={width}"
        if variant:
            query += f"&v={variant}"
        return f"/v1/studio/media/{asset_id}?{query}"

    def verify(self, asset_id: str, tenant: str, subject: str, expires: int, signature: str) -> bool:
        if expires < time.time():
            return False
        return hmac.compare_digest(self._digest(asset_id, tenant, subject, expires), signature)
