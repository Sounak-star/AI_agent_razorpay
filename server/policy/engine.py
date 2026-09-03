"""
Policy engine — the single entry point for all spend decisions.

evaluate() is pure, synchronous, deterministic, and has zero I/O.
The LLM never calls this; it is called by the payment service boundary
after mandate verification.

Rule execution order matters: first non-ALLOW verdict wins.
MANDATE_INVALID is always evaluated last so that a bad mandate doesn't
shadow a DENY that would have fired anyway.
"""

from __future__ import annotations

from typing import List, Optional

from server.mandate.schema import Cart, IntentMandate
from server.policy.codes import Decision, ReasonCode
from server.policy.rules import (
    ORDERED_RULES,
    TxnHistoryItem,
    Verdict,
    _ALLOW,
)


class EmptyCartError(ValueError):
    """Raised when evaluation is attempted without a cart. Never a verdict."""


def evaluate(
    intent: IntentMandate,
    cart: Cart,
    history: List[TxnHistoryItem],
    mandate_valid: bool = True,
    mandate_fail_reason: str = "",
) -> Verdict:
    """
    Evaluate all policy rules against the presented intent + cart + history.

    Parameters
    ----------
    intent          : The decoded IntentMandate (already signature-verified)
    cart            : The server-authoritative cart (prices from catalog, not LLM)
    history         : Past settled transactions for this buyer
    mandate_valid   : Result of verify_cart_mandate().valid
    mandate_fail_reason : Human-readable reason string on failure

    Returns
    -------
    Verdict with Decision.ALLOW, Decision.DENY, or Decision.ESCALATE
    and a machine-readable ReasonCode.
    """

    # A cartless session must never produce a policy decision.
    #
    # Every rule below is a comparison against the cart: no total to weigh, no
    # categories to check, no line items to count. An empty cart satisfies all
    # of them vacuously and would come out the far side carrying an ALLOW — a
    # verdict that means "checked and permitted" attached to something that was
    # never checked at all. Raising is the only safe answer; returning DENY
    # would still be a decision about a cart that does not exist.
    if cart is None or not getattr(cart, "items", None):
        raise EmptyCartError(
            "policy evaluation requires a cart with at least one item"
        )

    # MANDATE_INVALID is evaluated first — a bad mandate is a hard stop.
    if not mandate_valid:
        return Verdict(
            decision=Decision.DENY,
            code=ReasonCode.MANDATE_INVALID,
            detail=f"mandate verification failed: {mandate_fail_reason}",
        )

    # Run the ordered business rules
    for rule_fn in ORDERED_RULES:
        result: Optional[Verdict] = rule_fn(intent, cart, history)
        if result is not None:
            return result

    return _ALLOW
