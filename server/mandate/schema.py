"""
Mandate and cart schemas.

All monetary totals (total_paise) are computed server-side from item prices.
The LLM never produces a number that flows into a payment.
"""

from __future__ import annotations

import hashlib
import json
from typing import List, Optional

from pydantic import ConfigDict, Field, BaseModel, model_validator


class CartItem(BaseModel):
    sku_id: str
    name: str
    category: str
    quantity: int
    unit_price_paise: int  # server-authoritative price from catalog

    @property
    def line_total_paise(self) -> int:
        return self.quantity * self.unit_price_paise


class Cart(BaseModel):
    merchant_id: str
    items: List[CartItem]

    @property
    def total_paise(self) -> int:
        """Always server-computed. Never accepted from LLM or external input."""
        return sum(item.line_total_paise for item in self.items)

    @property
    def category_set(self) -> set[str]:
        return {item.category for item in self.items}

    def canonical_hash(self) -> str:
        """SHA-256 of the canonical (sorted-key, no-whitespace) JSON representation."""
        d = {
            "merchant_id": self.merchant_id,
            "items": [
                {
                    "sku_id": item.sku_id,
                    "quantity": item.quantity,
                    "unit_price_paise": item.unit_price_paise,
                }
                for item in sorted(self.items, key=lambda x: x.sku_id)
            ],
        }
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()


class IntentMandate(BaseModel):
    """Signed by the buyer before any cart exists."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    typ: str = "intent"
    jti: str                    # UUID, globally unique
    sub: str                    # buyer_agent_id
    aud: str                    # merchant_id
    iat: int                    # unix timestamp (seconds)
    exp: int                    # unix timestamp (seconds), max 15 min from iat
    budget_paise: int           # hard ceiling — policy engine enforces
    categories: List[str]       # allowlisted categories
    # Counts CART LINES, not units. A cart of 2 SKUs at quantity 5 each is 2
    # against this ceiling, not 10. "max_items" said neither, and read as units
    # to anyone who had not gone looking at rule_item_count.
    #
    # The alias keeps `max_items` valid on the wire: it is a claim inside signed
    # intent mandates, and renaming it outright would invalidate every mandate
    # already issued.
    max_line_items: int = Field(alias="max_items")
    estimate_paise: int         # buyer's expected total (used for drift check)

    @model_validator(mode="after")
    def _check_typ(self) -> "IntentMandate":
        if self.typ != "intent":
            raise ValueError(f"Expected typ='intent', got '{self.typ}'")
        return self


class CartMandate(BaseModel):
    """Signed by the buyer after seeing the concrete cart. References the intent."""

    typ: str = "cart"
    jti: str                    # UUID, globally unique
    intent_jti: str             # links back to the IntentMandate
    cart_hash: str              # sha256 of canonical cart (server verifies this)
    total_paise: int            # server-computed total (used for final amount check)
    iat: int                    # unix timestamp
    exp: int                    # unix timestamp, max 5 min from iat

    @model_validator(mode="after")
    def _check_typ(self) -> "CartMandate":
        if self.typ != "cart":
            raise ValueError(f"Expected typ='cart', got '{self.typ}'")
        return self


class VerifyResult(BaseModel):
    """Outcome of verify_cart_mandate()."""

    valid: bool
    reason: str = "ok"                              # machine-readable failure key
    cart_mandate_claims: Optional[dict] = None      # decoded claims on success
    intent_mandate_claims: Optional[dict] = None    # decoded intent claims on success
