"""
Mandate keypair manager and JWT signer.

Key lifecycle:
  - At boot, ensure_keypairs() checks whether PEM files exist at the configured
    paths. If not, it generates EC P-256 keypairs and saves them.
  - Keys are loaded lazily the first time sign_intent() / sign_cart() is called.
  - In production, mount the key files as secrets / volumes; never commit them.

Signing algorithm: ES256 (ECDSA P-256 / SHA-256), via python-jose.

IMPORTANT: This module only signs. Verification lives in verifier.py so that
the two halves can be tested independently and the signer never imports the
replay-prevention DB layer.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Optional

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from jose import jwt as jose_jwt
from jose.constants import ALGORITHMS

from server.config import settings
from server.mandate.schema import Cart, CartMandate, IntentMandate


# ── Key manager ───────────────────────────────────────────────────────────────

class KeyManager:
    """Manages one ES256 keypair (private + public PEM files)."""

    def __init__(self, private_path: str, public_path: str, label: str) -> None:
        self._private_path = Path(private_path)
        self._public_path = Path(public_path)
        self._label = label
        self._private_pem: Optional[str] = None
        self._public_pem: Optional[str] = None

    def ensure(self) -> None:
        """Generate keypair if files are absent; then load into memory."""
        if not self._private_path.exists() or not self._public_path.exists():
            self._generate()
        self._private_pem = self._private_path.read_text()
        self._public_pem = self._public_path.read_text()

    def _generate(self) -> None:
        self._private_path.parent.mkdir(parents=True, exist_ok=True)
        priv = generate_private_key(SECP256R1(), default_backend())
        priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        pub_pem = priv.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
        self._private_path.write_text(priv_pem)
        self._public_path.write_text(pub_pem)
        print(f"[tollgate] Generated EC P-256 keypair for '{self._label}': {self._private_path}")

    @property
    def private_pem(self) -> str:
        if self._private_pem is None:
            self.ensure()
        return self._private_pem

    @property
    def public_pem(self) -> str:
        if self._public_pem is None:
            self.ensure()
        return self._public_pem


# Module-level key managers — instantiated on import, loaded lazily on first use
buyer_keys = KeyManager(
    settings.BUYER_AGENT_PRIVATE_KEY_PATH,
    settings.BUYER_AGENT_PUBLIC_KEY_PATH,
    "buyer",
)
merchant_keys = KeyManager(
    settings.MERCHANT_AGENT_PRIVATE_KEY_PATH,
    settings.MERCHANT_AGENT_PUBLIC_KEY_PATH,
    "merchant",
)


def ensure_keypairs() -> None:
    """Called from FastAPI lifespan. Generates any missing keypairs."""
    buyer_keys.ensure()
    merchant_keys.ensure()


# ── Signer ────────────────────────────────────────────────────────────────────

def sign_intent(
    buyer_id: str,
    merchant_id: str,
    budget_paise: int,
    categories: list[str],
    max_items: int,
    estimate_paise: int,
    ttl_seconds: int = 900,          # 15-minute default
) -> tuple[str, IntentMandate]:
    """
    Sign an IntentMandate with the buyer's ES256 private key.

    Returns (jwt_token, parsed_mandate) so callers can log the mandate
    without re-decoding the JWT.
    """
    now = int(time.time())
    jti = str(uuid.uuid4())

    claims = {
        "typ": "intent",
        "jti": jti,
        "sub": buyer_id,
        "aud": merchant_id,
        "iat": now,
        "exp": now + ttl_seconds,
        "budget_paise": budget_paise,
        "categories": categories,
        "max_items": max_items,
        "estimate_paise": estimate_paise,
    }

    token = jose_jwt.encode(claims, buyer_keys.private_pem, algorithm=ALGORITHMS.ES256)
    mandate = IntentMandate(**claims)
    return token, mandate


def sign_cart(
    intent_jti: str,
    cart: Cart,
    ttl_seconds: int = 300,          # 5-minute default
) -> tuple[str, CartMandate]:
    """
    Sign a CartMandate with the buyer's ES256 private key.

    cart_hash is computed here from the canonical representation of cart.
    The server recomputes and compares at verify time.
    """
    now = int(time.time())
    jti = str(uuid.uuid4())
    cart_hash = cart.canonical_hash()

    claims = {
        "typ": "cart",
        "jti": jti,
        "intent_jti": intent_jti,
        "cart_hash": cart_hash,
        "total_paise": cart.total_paise,   # server-computed, logged for audit
        "iat": now,
        "exp": now + ttl_seconds,
    }

    token = jose_jwt.encode(claims, buyer_keys.private_pem, algorithm=ALGORITHMS.ES256)
    mandate = CartMandate(**claims)
    return token, mandate
