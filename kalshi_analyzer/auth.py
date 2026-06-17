"""Kalshi Trade API v2 RSA-PSS request authentication.

Authenticated endpoints (anything under ``/portfolio/*`` plus the bulk
``/markets/orderbooks`` and the WebSocket feed) require three headers:

  * ``KALSHI-ACCESS-KEY``      — your API key ID (UUID).
  * ``KALSHI-ACCESS-TIMESTAMP`` — current Unix time in **milliseconds**.
  * ``KALSHI-ACCESS-SIGNATURE`` — Base64-encoded RSA-PSS-SHA256
    signature over ``f"{timestamp}{method}{path}"`` (where ``path``
    includes any query string), using the RSA private key whose public
    half is registered for this key ID in Kalshi's dashboard.

This module loads the private key once and produces signed headers on
demand. It does **not** itself make HTTP calls — that's the
client/execution layer's job. Keeping the signer separate makes it
trivial to unit-test (we can sign a known message with a generated
key pair and verify it with the public half).
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignedHeaders:
    key_id: str
    timestamp_ms: str
    signature_b64: str

    def as_dict(self) -> dict[str, str]:
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": self.timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": self.signature_b64,
        }


class KalshiAuth:
    """Signs Trade API requests with RSA-PSS-SHA256."""

    def __init__(
        self,
        key_id: str,
        private_key: rsa.RSAPrivateKey,
    ) -> None:
        if not key_id:
            raise ValueError("KalshiAuth requires a non-empty key_id")
        self.key_id = key_id
        self._private_key = private_key

    @classmethod
    def from_pem_path(cls, key_id: str, pem_path: str | Path) -> "KalshiAuth":
        pem_bytes = Path(pem_path).read_bytes()
        return cls.from_pem(key_id, pem_bytes)

    @classmethod
    def from_pem(cls, key_id: str, pem_bytes: bytes) -> "KalshiAuth":
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Kalshi keys must be RSA private keys")
        return cls(key_id=key_id, private_key=private_key)

    def sign(self, method: str, path: str, *, timestamp_ms: int | None = None) -> SignedHeaders:
        """Sign one request and return headers.

        ``path`` MUST include any query string verbatim — Kalshi signs
        ``timestamp + method + path`` and rejects mismatches.
        """

        ts_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        ts_str = str(ts_ms)
        msg = f"{ts_str}{method.upper()}{path}".encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return SignedHeaders(
            key_id=self.key_id,
            timestamp_ms=ts_str,
            signature_b64=base64.b64encode(sig).decode("ascii"),
        )

    def signed_headers(
        self, method: str, path: str, *, timestamp_ms: int | None = None
    ) -> dict[str, str]:
        return self.sign(method, path, timestamp_ms=timestamp_ms).as_dict()


def load_auth_from_settings(settings_obj) -> KalshiAuth | None:
    """Build a ``KalshiAuth`` from ``settings`` if credentials are present.

    Returns ``None`` when ``KALSHI_KEY_ID`` or ``KALSHI_PRIVATE_KEY_PATH``
    is empty — callers should treat that as "auth disabled" and fall
    back to public endpoints only.
    """

    key_id = (settings_obj.kalshi_key_id or "").strip()
    pem_path = (settings_obj.kalshi_private_key_path or "").strip()
    if not key_id or not pem_path:
        return None
    if not Path(pem_path).exists():
        log.warning(
            "KALSHI_PRIVATE_KEY_PATH=%s does not exist; running unauthenticated",
            pem_path,
        )
        return None
    try:
        return KalshiAuth.from_pem_path(key_id, pem_path)
    except Exception as exc:
        log.error("failed to load Kalshi RSA key from %s: %s", pem_path, exc)
        return None
