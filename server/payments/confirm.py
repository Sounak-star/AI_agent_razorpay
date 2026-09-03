"""
Waiting for a payment to be captured.

The saga does not know how confirmation arrives. It asks a confirmer to wait
and gets back a CaptureResult; whether that came from polling the API, from a
webhook, or from a recorded fixture is the confirmer's business.

That indirection is the point. Polling is used today because it needs no public
tunnel — a demo cannot depend on an inbound URL reaching a laptop. When a
webhook receiver is available it implements this same protocol and the saga is
untouched: swap the confirmer, not the payment path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from server.payments import fixtures
from server.payments.razorpay_client import RazorpayError, fetch_payment_link

# Razorpay reports a link as "paid" once a payment against it is captured.
PAID_STATUSES = frozenset({"paid"})
DEAD_STATUSES = frozenset({"cancelled", "expired"})


@dataclass(frozen=True)
class CaptureResult:
    """The outcome of waiting. `payment_id` is set only when captured."""
    captured: bool
    payment_id: str | None
    status: str
    waited_seconds: float
    detail: str | None = None
    raw: dict | None = None


class PaymentConfirmer(Protocol):
    """Anything that can tell the saga a link has been paid."""

    def wait_for_capture(
        self, payment_link_id: str, *, timeout_seconds: float
    ) -> CaptureResult: ...


def _payment_id_from_link(link: dict) -> str | None:
    """
    Pull the captured payment id out of a payment-link response.

    The link carries a `payments` array; the captured entry is the one that
    matters, and its `payment_id` is what the refund path will later need.
    """
    for entry in link.get("payments") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") == "captured" and entry.get("payment_id"):
            return entry["payment_id"]
    # Some responses expose it directly.
    return link.get("payment_id") or None


class PollingConfirmer:
    """
    Poll the Payment Link until it reports paid.

    Deliberately not a webhook: a stage demo cannot rely on an inbound URL
    reaching the machine it is running on. The trade is latency, bounded by
    `interval_seconds`, against a dependency on public reachability.
    """

    def __init__(self, interval_seconds: float = 2.0) -> None:
        self.interval_seconds = interval_seconds

    def wait_for_capture(
        self, payment_link_id: str, *, timeout_seconds: float
    ) -> CaptureResult:
        started = time.monotonic()
        last_status = "unknown"
        last: dict = {}

        while True:
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds:
                return CaptureResult(
                    captured=False,
                    payment_id=None,
                    status=last_status,
                    waited_seconds=elapsed,
                    detail=f"no capture within {timeout_seconds:.0f}s",
                    raw=last or None,
                )

            try:
                last = fetch_payment_link(payment_link_id)
                last_status = str(last.get("status") or "unknown")
            except RazorpayError as exc:
                # A transient read failure is not a failed payment. Keep
                # waiting; the deadline is what ends this loop.
                last_status = "unreadable"
                last = {"error": exc.as_payload()}

            if last_status in PAID_STATUSES:
                return CaptureResult(
                    captured=True,
                    payment_id=_payment_id_from_link(last),
                    status=last_status,
                    waited_seconds=time.monotonic() - started,
                    raw=last,
                )

            if last_status in DEAD_STATUSES:
                return CaptureResult(
                    captured=False,
                    payment_id=None,
                    status=last_status,
                    waited_seconds=time.monotonic() - started,
                    detail=f"payment link is {last_status}",
                    raw=last,
                )

            time.sleep(self.interval_seconds)


class FixtureConfirmer:
    """Replay a recorded capture. Never touches the network."""

    def wait_for_capture(
        self, payment_link_id: str, *, timeout_seconds: float
    ) -> CaptureResult:
        payment = fixtures.get("payment")
        link = fixtures.get("payment_link")
        body = (payment or {}).get("response") or {}
        link_body = (link or {}).get("response") or {}

        payment_id = body.get("id") or _payment_id_from_link(link_body)
        return CaptureResult(
            captured=bool(payment_id),
            payment_id=payment_id,
            status="paid" if payment_id else "no_fixture",
            waited_seconds=0.0,
            detail=None if payment_id else "no recorded payment in the fixture",
            raw=body or None,
        )


class SyntheticConfirmer:
    """
    Generate a capture locally. No network, and never badged REPLAYED.

    This is what keeps a complete demo possible with Razorpay unreachable.
    """

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id

    def wait_for_capture(
        self, payment_link_id: str, *, timeout_seconds: float
    ) -> CaptureResult:
        return CaptureResult(
            captured=True,
            payment_id=f"harness_pay_{self.session_id[:8]}",
            status="paid",
            waited_seconds=0.0,
            detail="synthetic capture — no provider was contacted",
        )
