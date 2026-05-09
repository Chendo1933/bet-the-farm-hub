"""
Kalshi v2 API request signing.

Each request signs `timestamp_ms + METHOD + path` with an RSA-PSS-SHA256
signature using the user's private key. Three headers go on every request:
  KALSHI-ACCESS-KEY        the user's API key UUID
  KALSHI-ACCESS-SIGNATURE  base64(rsa_pss_sha256(message, private_key))
  KALSHI-ACCESS-TIMESTAMP  current ms-since-epoch as string

The private key is read from either:
  1. KALSHI_PRIVATE_KEY environment variable (PEM-encoded string)
  2. KALSHI_PRIVATE_KEY_PATH environment variable (path to .pem file)

Never check private keys into the repo.
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError as e:
    raise ImportError(
        "cryptography library required: pip install cryptography"
    ) from e


def load_private_key():
    """
    Load the RSA private key from KALSHI_PRIVATE_KEY (PEM string) or
    KALSHI_PRIVATE_KEY_PATH (file path). Returns a private key object.
    Raises a clear error if neither is set or the key is invalid.
    """
    pem = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if pem:
        pem_bytes = pem.encode()
    elif path:
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(
                f"KALSHI_PRIVATE_KEY_PATH points to {p} but no file exists there"
            )
        pem_bytes = p.read_bytes()
    else:
        raise RuntimeError(
            "Neither KALSHI_PRIVATE_KEY nor KALSHI_PRIVATE_KEY_PATH is set. "
            "Set one to your RSA private key (PEM) — see scripts/kalshi/README.md"
        )
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as e:
        raise RuntimeError(
            f"Could not parse private key — verify it's PEM-formatted and RSA: {e}"
        )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise RuntimeError(
            "Loaded key is not RSA — Kalshi requires RSA. Re-generate with `openssl genrsa -out kalshi.pem 2048`"
        )
    return key


def get_api_key_id() -> str:
    """Read the public API key ID (UUID) from env. Required for every request."""
    key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
    if not key_id:
        raise RuntimeError(
            "KALSHI_API_KEY_ID env var not set. Get the UUID from your Kalshi account: "
            "Settings → API Keys → 'Key ID'"
        )
    return key_id


def sign_request(private_key, method: str, path: str, timestamp_ms: int | None = None):
    """
    Sign a Kalshi v2 API request.

    Returns: (signature_b64, timestamp_str) tuple.
    The signature goes in KALSHI-ACCESS-SIGNATURE, timestamp in KALSHI-ACCESS-TIMESTAMP.
    """
    if timestamp_ms is None:
        timestamp_ms = int(time.time() * 1000)
    # Kalshi message format: timestamp_ms + METHOD + path (path includes query string)
    message = f"{timestamp_ms}{method.upper()}{path}".encode()
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode(), str(timestamp_ms)


def auth_headers(private_key, key_id: str, method: str, path: str) -> dict:
    """Build the three Kalshi auth headers for a single request."""
    sig, ts = sign_request(private_key, method, path)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }
