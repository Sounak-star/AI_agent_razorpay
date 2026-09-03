"""
Record the real Razorpay test-mode responses into evals/fixtures/razorpay_capture.json.

Run once, against test mode, with the ids of a capture that actually happened.
Everything it writes is the provider's own response body, unedited. Nothing here
constructs a plausible-looking payload: a fixture written by hand is not evidence
of anything, and the REPLAYED badge would then be a claim with nothing behind it.

    python scripts/record_fixture.py \
        --order order_XXX --link plink_XXX --payment pay_XXX [--refund]

--refund attempts a REAL refund of the captured amount. In test mode that moves
no real money, and the response — accepted or rejected — is recorded verbatim.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from server.payments import fixtures  # noqa: E402
from server.payments.razorpay_client import (  # noqa: E402
    RazorpayError,
    _client,
    create_refund,
    fetch_payment,
    fetch_payment_link,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order", required=True)
    ap.add_argument("--link", required=True)
    ap.add_argument("--payment", required=True)
    ap.add_argument("--refund", action="store_true")
    args = ap.parse_args()

    print(f"fixture -> {fixtures.FIXTURE_PATH}\n")

    # Recording is off by default, so that a live run somewhere else cannot
    # overwrite the capture as a side effect. This script is the deliberate act
    # that turns it on.
    with fixtures.recording():
        rc = _record(args)

    problem = fixtures.coherence_problem()
    if problem:
        print(f"\n  [WARN] this recording is not internally coherent:\n"
              f"         {problem}\n"
              f"         Replay will refuse it and fall back to synthetic.")
        return 1
    print("\n  coherence: order, link and payment describe one transaction")
    return rc


def _record(args) -> int:
    # Order. Fetched rather than created: this is the order that was actually paid.
    try:
        order = _client().order.fetch(args.order)
        fixtures.record("order", order, ok=True, status_code=200)
        print(f"  order          {order.get('id')}  {order.get('amount')} "
              f"{order.get('currency')}  status={order.get('status')}")
    except Exception as exc:
        print(f"  order          FAILED: {exc}")

    # fetch_payment_link and fetch_payment record themselves.
    try:
        link = fetch_payment_link(args.link)
        print(f"  payment_link   {link.get('id')}  status={link.get('status')}  "
              f"{link.get('short_url')}")
    except RazorpayError as exc:
        print(f"  payment_link   FAILED: {exc}")

    payment = None
    try:
        payment = fetch_payment(args.payment)
        print(f"  payment        {payment.get('id')}  status={payment.get('status')}  "
              f"amount={payment.get('amount')}  captured={payment.get('captured')}")
    except RazorpayError as exc:
        print(f"  payment        FAILED: {exc}")

    if args.refund:
        amount = (payment or {}).get("amount") or 100
        print(f"\n  attempting REAL refund of {amount} paise on {args.payment}")
        try:
            refund = create_refund(payment_id=args.payment, amount_paise=int(amount))
            print(f"  refund         ACCEPTED {refund.get('id')}")
        except RazorpayError as exc:
            # Recorded by create_refund before it re-raised. A rejection is the
            # documented outcome here, not a failure of this script.
            print(f"  refund         REJECTED status={exc.status_code} "
                  f"code={exc.code}\n                 body={json.dumps(exc.body)}")

    print("\nrecorded kinds:", json.dumps(fixtures.summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
