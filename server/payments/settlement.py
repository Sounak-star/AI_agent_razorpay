"""
Whether a captured payment has settled, and when it is expected to.

This exists because of a specific failure. Refunding a captured payment returns
HTTP 400 BAD_REQUEST_ERROR with the description "invalid request sent" and empty
metadata — an error body that says nothing about why. The actual cause, per
Razorpay's own dashboard, is:

    "Your account does not have sufficient balance to instantly refund this
     payment."

The payment is captured but not yet settled, so there is no balance to refund
from. That is a timing constraint, not a malformed request and not a defect in
this codebase, and the generic error body is what hides the difference.

The distinction matters operationally: a refund that cannot happen *yet* is
retryable after settlement, while a refund that was genuinely rejected is not.
Recording both as the same failure would either strand a buyer who is owed money
or bury a real fault under a retry loop.

Two fields, two different kinds of certainty, labelled as such:

  settlement_status     Queried. /v1/settlements is the provider's own answer.
  expected_settlement   Derived. Razorpay exposes no per-payment settlement
                        schedule endpoint (/v1/payments/{id}/settlement is 404),
                        so this is computed from the capture time and the
                        account's settlement cycle, and carries the basis it was
                        computed from so nobody reads it as a quoted fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone

from server.config import settings

log = logging.getLogger(__name__)

# Razorpay's generic rejection. The body carries no code for the balance
# constraint, so the constraint has to be established from the payment's
# settlement state rather than read off the error.
_GENERIC_REJECTIONS = {"invalid request sent"}


@dataclass(frozen=True)
class SettlementState:
    """What is known about a payment's settlement, and how it is known."""

    status: str                       # settled | unsettled | unknown
    status_source: str                # how `status` was established
    expected_at: str | None           # ISO date, derived
    expected_basis: str | None        # what `expected_at` was computed from
    settled_count: int | None = None  # settlements the account reported

    def as_payload(self) -> dict:
        return asdict(self)


def expected_settlement_date(captured_at: datetime | None) -> tuple[str | None, str | None]:
    """
    When this payment should settle, and the basis for saying so.

    Derived, never quoted. Returns (iso_date, basis) so the caller records both
    and a reader can tell a computed date from a provider-supplied one.
    """
    if captured_at is None:
        return None, None
    days = settings.SETTLEMENT_CYCLE_DAYS
    due = (captured_at.astimezone(timezone.utc) + timedelta(days=days)).date()
    return (
        due.isoformat(),
        f"capture date {captured_at.astimezone(timezone.utc).date().isoformat()} "
        f"+ T+{days} settlement cycle (derived; Razorpay exposes no per-payment "
        f"settlement schedule endpoint)",
    )


def settlement_state(captured_at: datetime | None = None) -> SettlementState:
    """
    Ask the provider what has settled, and derive when this payment will.

    A query failure returns status "unknown" rather than guessing "unsettled".
    Assuming unsettled would make every refund rejection look retryable, which
    is precisely the mistake that leaves a real failure sitting in a retry loop
    while the buyer waits.
    """
    expected_at, basis = expected_settlement_date(captured_at)

    try:
        from server.payments.razorpay_client import list_settlements

        settlements = list_settlements()
        count = len(settlements.get("items") or [])
        return SettlementState(
            # Nothing has settled on this account, so a payment captured today
            # certainly has not. This is the provider's own answer, not an
            # inference from the error body.
            status="unsettled" if count == 0 else "unknown",
            status_source=(
                f"GET /v1/settlements returned {count} settlements"
                + ("; none cover this payment" if count == 0 else
                   "; per-payment attribution unavailable from this endpoint")
            ),
            expected_at=expected_at,
            expected_basis=basis,
            settled_count=count,
        )
    except Exception as exc:                                  # noqa: BLE001
        log.warning("[settlement] could not query settlements: %s", exc)
        return SettlementState(
            status="unknown",
            status_source=f"settlement query failed: {exc}",
            expected_at=expected_at,
            expected_basis=basis,
        )


def is_pending_settlement(
    *, status_code: int | None, body: object, state: SettlementState
) -> bool:
    """
    Is this rejection the balance constraint rather than a real refusal?

    Both halves are required. The error body alone cannot tell them apart — it
    is the same generic 400 either way — so the payment's settlement state is
    what carries the weight. If settlement status could not be established, this
    is not claimed: an unverified "it'll work later" is worse than admitting the
    cause is unknown, because it silently converts a permanent failure into an
    endless retry.
    """
    if status_code != 400 or state.status != "unsettled":
        return False

    description = ""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            description = str(error.get("description") or "").strip().lower()
    return description in _GENERIC_REJECTIONS
