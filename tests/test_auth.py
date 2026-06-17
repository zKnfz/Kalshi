"""Verifies that ``KalshiAuth`` produces signatures that round-trip with
the matching public key, using a freshly generated 2048-bit key pair.

If this test breaks it means the PSS parameters / digest / message
format have drifted from what Kalshi's verifier expects, which would
cause every authenticated request to bounce with HTTP 401."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_analyzer.auth import KalshiAuth


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    return priv, pub


def test_signed_headers_round_trip(keypair):
    priv, pub = keypair
    auth = KalshiAuth(key_id="test-key", private_key=priv)
    headers = auth.signed_headers("GET", "/trade-api/v2/portfolio/balance")
    assert headers["KALSHI-ACCESS-KEY"] == "test-key"
    ts = headers["KALSHI-ACCESS-TIMESTAMP"]
    assert ts.isdigit() and len(ts) >= 10

    sig = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    msg = f"{ts}GET/trade-api/v2/portfolio/balance".encode("utf-8")
    pub.verify(
        sig,
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_path_with_query_string_is_signed_verbatim(keypair):
    priv, _ = keypair
    auth = KalshiAuth(key_id="k", private_key=priv)
    h1 = auth.sign("GET", "/portfolio/positions?limit=100", timestamp_ms=1000)
    h2 = auth.sign("GET", "/portfolio/positions", timestamp_ms=1000)
    assert h1.signature_b64 != h2.signature_b64


def test_timestamp_changes_signature(keypair):
    priv, _ = keypair
    auth = KalshiAuth(key_id="k", private_key=priv)
    h1 = auth.sign("GET", "/portfolio/balance", timestamp_ms=1000)
    h2 = auth.sign("GET", "/portfolio/balance", timestamp_ms=2000)
    assert h1.signature_b64 != h2.signature_b64


def test_from_pem_roundtrip(tmp_path):
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_path = tmp_path / "k.pem"
    pem_path.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    auth = KalshiAuth.from_pem_path("kid", str(pem_path))
    assert auth.key_id == "kid"
    headers = auth.signed_headers("POST", "/portfolio/orders")
    assert "KALSHI-ACCESS-SIGNATURE" in headers


def test_blank_key_id_raises():
    with pytest.raises(ValueError):
        KalshiAuth(
            key_id="",
            private_key=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        )
