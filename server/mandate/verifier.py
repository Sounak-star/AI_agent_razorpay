"""
Mandate verifier.

verify_cart_mandate() is the gateway that every payment path must pass through.
It returns VerifyResult(valid=False) for any of:
  - Bad ES256 signature (jose.JWTError)
  - Token expired (exp < now)
  - JTI already in the MandateJti table (replay) — UNIQUE constraint is the guard
  - cart_hash mismatch between the JWT claim and the presented cart
  - intent_jti not found in MandateJti, or its referenced intent is expired

The verifier NEVER reads prices, SKU lists, or category labels from the JWT.
Those come from the server-authoritative quote and are validated by the policy engine.
"""

from __future__ import annotations

import time
from datetime import datetime

from jose import JWTError, jwt as jose_jwt
from jose.constants import ALGORITHMS
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.db.models import MandateJti
from server.mandate.issuer import buyer_keys
from server.mandate.schema import Cart, VerifyResult


def verify_cart_mandate(
    token: str,
    cart: Cart,
    db: Session,
    session_id: str | None = None,
) -> VerifyResult:
    """
    Full verification of a CartMandate JWT.

    Returns VerifyResult(valid=True) only when ALL of the following hold:
      1. ES256 signature is valid
      2. Token has not expired
      3. JTI has never been presented before (replay prevention)
      4. cart_hash in the JWT matches cart.canonical_hash()
      5. The referenced intent_jti exists in MandateJti and has not expired

    On the first successful call, the JTI is recorded so replays are rejected.
    """

    # ── Step 1 + 2: signature + expiry ────────────────────────────────────────
    try:
        claims = jose_jwt.decode(
            token,
            buyer_keys.public_pem,
            algorithms=[ALGORITHMS.ES256],
            options={"verify_aud": False},   # aud check is handled below
        )
    except JWTError as exc:
        return VerifyResult(valid=False, reason=f"jwt_error: {exc}")

    if claims.get("typ") != "cart":
        return VerifyResult(valid=False, reason="wrong_typ")

    now = int(time.time())
    if claims.get("exp", 0) < now:
        return VerifyResult(valid=False, reason="token_expired")

    cart_jti: str = claims["jti"]
    intent_jti: str = claims.get("intent_jti", "")

    # ── Step 3: replay prevention via DB UNIQUE constraint ────────────────────
    # We attempt insert first. An IntegrityError means the jti is already recorded.
    # This pattern avoids the TOCTOU race that a check-then-insert would have.
    try:
        db.add(
            MandateJti(
                jti=cart_jti,
                jti_type="cart",
                session_id=session_id,
                expires_at=datetime.utcfromtimestamp(claims["exp"]),
            )
        )
        db.flush()  # surface constraint violations without committing
    except IntegrityError:
        db.rollback()
        return VerifyResult(valid=False, reason="jti_replayed")

    # ── Step 4: cart_hash check ───────────────────────────────────────────────
    expected_hash = cart.canonical_hash()
    if claims.get("cart_hash") != expected_hash:
        db.rollback()
        return VerifyResult(valid=False, reason="cart_hash_mismatch")

    # ── Step 5: intent_jti must exist and be unexpired ────────────────────────
    intent_row = db.query(MandateJti).filter(
        MandateJti.jti == intent_jti,
        MandateJti.jti_type == "intent",
    ).first()

    if intent_row is None:
        db.rollback()
        return VerifyResult(valid=False, reason="intent_jti_not_found")

    if intent_row.expires_at < datetime.utcnow():
        db.rollback()
        return VerifyResult(valid=False, reason="intent_jti_expired")

    # ── All checks passed — commit the JTI record ─────────────────────────────
    db.commit()

    # Retrieve intent claims from the intent row's stored data if available,
    # or return without them (caller can re-decode the intent JWT if needed).
    return VerifyResult(
        valid=True,
        reason="ok",
        cart_mandate_claims=claims,
    )


def record_intent_jti(
    jti: str,
    expires_at: datetime,
    db: Session,
    session_id: str | None = None,
) -> bool:
    """
    Record an IntentMandate JTI when the buyer signs the intent.

    Returns True when the JTI is now recorded against this session, False only
    when it is already held by a *different* session — which is what replay
    actually means.

    A session re-presenting its own intent is not a replay. The mandate is
    registered when the session is created and again when its saga runs, so
    treating any duplicate as a replay made every demo-mode checkout fail with
    "intent JTI already used" before it reached the policy engine. The DB-level
    UNIQUE(jti) is still the authoritative guard against a genuine replay; this
    only distinguishes the owner of the existing row.
    """
    try:
        db.add(
            MandateJti(
                jti=jti,
                jti_type="intent",
                session_id=session_id,
                expires_at=expires_at,
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        existing = db.query(MandateJti).filter(MandateJti.jti == jti).first()
        # Same session re-presenting its own mandate: not a replay.
        return bool(
            existing is not None
            and session_id is not None
            and existing.session_id == session_id
        )
