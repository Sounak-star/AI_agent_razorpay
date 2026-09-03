"""
Policy rules — one pure function per rule.

Contract:
  - Each rule takes (intent: IntentMandate, cart: Cart, history: list[TxnHistoryItem])
    and returns Optional[Verdict].
  - Returning None means "this rule does not apply / passes".
  - engine.evaluate() calls rules in order; the first non-None result wins.
  - Zero I/O, zero LLM calls, zero network. Fully deterministic.
  - All configurable thresholds come from settings (loaded at import time).

The injection-defence story:
  These rules are the reason an injected discount instruction cannot move money.
  Even if the LLM "obeys" the injection and proposes price=0, the server-computed
  cart.total_paise is what the rules see. The LLM cannot overwrite it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional

from server.config import settings
from server.mandate.schema import Cart, IntentMandate
from server.policy.codes import Decision, ReasonCode


# ── Supporting types ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TxnHistoryItem:
    """Immutable snapshot of a past settled transaction (passed in by the engine)."""
    session_id: str
    merchant_id: str
    total_paise: int
    settled: bool
    ts: float           # unix timestamp


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    code: ReasonCode
    detail: str


_ALLOW = Verdict(decision=Decision.ALLOW, code=ReasonCode.ALLOW, detail="all rules passed")


# ── Individual rules ──────────────────────────────────────────────────────────

def rule_per_txn_cap(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """DENY if cart.total exceeds intent.budget_paise."""
    if cart.total_paise > intent.budget_paise:
        return Verdict(
            decision=Decision.DENY,
            code=ReasonCode.PER_TXN_CAP,
            detail=(
                f"cart total {cart.total_paise} paise exceeds "
                f"intent budget {intent.budget_paise} paise"
            ),
        )
    return None


def rule_daily_cap(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """DENY if today's settled spend + cart total would exceed the daily cap."""
    today_start = _start_of_day_ts()
    today_total = sum(
        h.total_paise for h in history if h.settled and h.ts >= today_start
    )
    if today_total + cart.total_paise > settings.DAILY_SPEND_CAP_PAISE:
        return Verdict(
            decision=Decision.DENY,
            code=ReasonCode.DAILY_CAP,
            detail=(
                f"today's settled spend ({today_total}) + cart ({cart.total_paise}) "
                f"= {today_total + cart.total_paise} paise exceeds daily cap "
                f"{settings.DAILY_SPEND_CAP_PAISE} paise"
            ),
        )
    return None


def rule_velocity(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """ESCALATE if there are too many transactions in the rolling window."""
    window_start = time.time() - settings.VELOCITY_WINDOW_SECONDS
    recent = [h for h in history if h.ts >= window_start]
    if len(recent) >= settings.VELOCITY_MAX_TXN:
        return Verdict(
            decision=Decision.ESCALATE,
            code=ReasonCode.VELOCITY,
            detail=(
                f"{len(recent)} transactions in the last "
                f"{settings.VELOCITY_WINDOW_SECONDS}s "
                f"(limit: {settings.VELOCITY_MAX_TXN})"
            ),
        )
    return None


def rule_category_deny(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """DENY if any SKU's category is outside the intent allowlist."""
    allowed = set(intent.categories)
    for item in cart.items:
        if item.category not in allowed:
            return Verdict(
                decision=Decision.DENY,
                code=ReasonCode.CATEGORY_DENY,
                detail=(
                    f"SKU '{item.sku_id}' has category '{item.category}' "
                    f"which is not in intent allowlist {sorted(allowed)}"
                ),
            )
    return None


def rule_price_drift(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """ESCALATE if cart total is more than 5% above the buyer's estimate."""
    if intent.estimate_paise <= 0:
        return None
    ceiling = int(intent.estimate_paise * 1.05)
    if cart.total_paise > ceiling:
        drift_pct = (cart.total_paise - intent.estimate_paise) / intent.estimate_paise * 100
        return Verdict(
            decision=Decision.ESCALATE,
            code=ReasonCode.PRICE_DRIFT,
            detail=(
                f"cart total {cart.total_paise} paise is "
                f"{drift_pct:.1f}% above estimate {intent.estimate_paise} paise "
                f"(5% ceiling: {ceiling})"
            ),
        )
    return None


def rule_item_count(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """
    DENY if the cart has more LINE ITEMS than the intent allows.

    Lines, not units: a cart of 2 SKUs at quantity 5 each counts as 2. The unit
    total is reported in the detail so the distinction is visible at the point
    of decision rather than needing to be inferred from the rule's source.

    There is deliberately no separate ceiling on units. Total spend is already
    bounded by per_txn_cap and daily_cap, so quantity is constrained by value
    rather than by count. If a distinct unit ceiling is ever wanted it is a new
    rule, not a change of meaning for this one.
    """
    lines = len(cart.items)
    units = sum(i.quantity for i in cart.items)
    if lines > intent.max_line_items:
        return Verdict(
            decision=Decision.DENY,
            code=ReasonCode.ITEM_COUNT,
            detail=(
                f"cart has {lines} line items ({units} units); "
                f"intent allows {intent.max_line_items} line items"
            ),
        )
    return None


def rule_first_contact_buyer(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
) -> Optional[Verdict]:
    """
    ESCALATE when this buyer has no SETTLED transaction with this merchant.

    Scoped to the buyer, not the merchant: `history` is built per buyer at every
    call site, so this is a first-contact check for that pair. It is not a claim
    that the merchant is new to the platform — a merchant with thousands of
    settled transactions still trips this for a buyer who has never used it.

    The settled filter was previously missing while the docstring claimed it,
    so an unsettled transaction would have satisfied the rule. Every caller
    happens to pass settled=True today, which is exactly why the gap was
    invisible.
    """
    prior_with_merchant = [
        h for h in history if h.settled and h.merchant_id == cart.merchant_id
    ]
    if not prior_with_merchant:
        return Verdict(
            decision=Decision.ESCALATE,
            code=ReasonCode.FIRST_CONTACT_BUYER,
            detail=f"buyer has no settled transaction with merchant '{cart.merchant_id}'",
        )
    return None


# ── Ordered rule list (engine iterates this) ──────────────────────────────────

ORDERED_RULES = [
    rule_per_txn_cap,
    rule_daily_cap,
    rule_velocity,
    rule_category_deny,
    rule_price_drift,
    rule_item_count,
    rule_first_contact_buyer,
    # rule_mandate_invalid is handled directly in engine.evaluate()
    # because it receives the VerifyResult, not just (intent, cart, history)
]


# ── Helper ────────────────────────────────────────────────────────────────────

def _start_of_day_ts() -> float:
    """Unix timestamp for 00:00:00 UTC today."""
    import datetime
    today = datetime.datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return today.timestamp()
