"""
Recorded Razorpay responses.

Every live API response is written here verbatim — the whole body, exactly as
the provider returned it. Harness runs replay these instead of calling the API,
and only events backed by this file may be badged REPLAYED. Anything generated
locally stays SYNTHETIC, because "replayed from a real capture" and "made up so
the demo runs" are different claims and the dashboard must not blur them.

The failed refund is recorded like any other response. A rejection is a real
result, and the one most worth keeping: it is evidence of what the provider
actually said, rather than a summary of it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FIXTURE_PATH = Path(__file__).parent.parent.parent / "evals" / "fixtures" / "razorpay_capture.json"

# The API calls a live run makes, in the order it makes them.
KINDS = ("order", "payment_link", "payment", "refund_attempt")


def _load_raw() -> dict:
    if not FIXTURE_PATH.exists():
        return {}
    try:
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# Recording is off unless something deliberately turns it on.
#
# It used to happen as a side effect of every live API call, which quietly
# corrupted the recording: a live payment-link test overwrote the stored order
# and link, leaving an unpaid Rs.799 order sitting beside a Rs.1 payment from a
# different order. The three parts were never one transaction, but a replay of
# them still carried a REPLAYED badge.
#
# A fixture is a coherent record of one capture, so it is written in one
# deliberate act — scripts/record_fixture.py — and not by whatever ran last.
_recording = False


def recording(enabled: bool = True):
    """Context manager enabling fixture writes for a deliberate recording run."""
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        global _recording
        previous = _recording
        _recording = enabled
        try:
            yield
        finally:
            _recording = previous

    return _scope()


def record(kind: str, response: Any, *, ok: bool = True, status_code: int | None = None) -> None:
    """
    Store one API response verbatim, keyed by kind.

    Only writes inside a `recording()` scope. Never raises: a recording failure
    must not be able to fail a payment that has already happened.
    """
    if kind not in KINDS or not _recording:
        return
    try:
        data = _load_raw()
        data.setdefault("_meta", {})
        data["_meta"]["recorded_at"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        data["_meta"]["source"] = "razorpay test mode"
        data[kind] = {
            "ok": ok,
            "status_code": status_code,
            "response": response,
        }
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:                                 # noqa: BLE001 — see docstring
        pass


def get(kind: str) -> dict | None:
    """The recorded response for one kind, or None if never captured."""
    entry = _load_raw().get(kind)
    if not isinstance(entry, dict) or "response" not in entry:
        return None
    return entry


def has(kind: str) -> bool:
    return get(kind) is not None


def fixture_path_if_backed(kind: str) -> str | None:
    """
    The fixture path, but only when this kind is actually recorded.

    Returning the path unconditionally is how an event ends up claiming to be
    replayed from a capture that does not contain it.

    Repo-relative, not absolute. This string is hashed into a ledger entry and
    shown on screen; whose laptop it was recorded on is not part of the record,
    and an absolute path is not portable to anyone reading the chain elsewhere.
    """
    if not has(kind):
        return None
    root = FIXTURE_PATH.parent.parent.parent
    try:
        return FIXTURE_PATH.relative_to(root).as_posix()
    except ValueError:
        return FIXTURE_PATH.as_posix()


def recorded_amount_paise(kind: str = "payment") -> int | None:
    """The amount the recorded capture was actually for."""
    body = (get(kind) or {}).get("response") or {}
    amount = body.get("amount")
    return int(amount) if isinstance(amount, (int, float)) else None


def summary() -> dict:
    """What has been captured, for the README and the eval report."""
    data = _load_raw()
    return {
        "path": str(FIXTURE_PATH),
        "exists": FIXTURE_PATH.exists(),
        "recorded_at": (data.get("_meta") or {}).get("recorded_at"),
        "kinds": {k: has(k) for k in KINDS},
    }


def coherence_problem() -> str | None:
    """
    Why this recording cannot be replayed as one transaction, or None if it can.

    The three parts have to belong together. A recording assembled from
    different transactions still satisfies "every field came from the real API"
    while describing a payment that never happened, and that is exactly the
    claim a REPLAYED badge makes.
    """
    order = (get("order") or {}).get("response") or {}
    link = (get("payment_link") or {}).get("response") or {}
    payment = (get("payment") or {}).get("response") or {}

    if not order or not payment:
        return None                      # nothing recorded; nothing to contradict

    order_id = order.get("id")
    paid_order = payment.get("order_id")
    if order_id and paid_order and order_id != paid_order:
        return (
            f"payment {payment.get('id')} belongs to order {paid_order}, "
            f"but the recorded order is {order_id}"
        )

    if payment.get("status") != "captured":
        return f"recorded payment is {payment.get('status')}, not captured"

    link_status = link.get("status")
    if link and link_status not in ("paid", None):
        return f"recorded payment link is {link_status}, not paid"

    return None
