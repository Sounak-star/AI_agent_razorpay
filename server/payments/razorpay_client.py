"""
Razorpay thin-wrapper client.

Wraps the official razorpay SDK. All methods return typed dicts or raise
RazorpayError. Callers NEVER import razorpay directly; they go through here.

STUB MODE: when settings.STUB_MODE is True (or the module is imported in a
test with STUB_MODE=true in the env), every method raises StubNotImplemented
unless the call is coming from the eval harness replay path.

The harness reads from evals/fixtures/razorpay_capture.json and passes the
recorded IDs directly; it never calls this client with real IDs.
"""

from __future__ import annotations

import razorpay
import requests

from server.config import settings
from server.payments import fixtures

_API_BASE = "https://api.razorpay.com/v1"
_HTTP_TIMEOUT_SECONDS = 30


class RazorpayError(Exception):
    """
    A Razorpay API call failed.

    Carries the provider's response verbatim rather than a formatted message.
    A refund rejection has to be recorded exactly as it was returned — a
    paraphrase is not evidence, and the body is the only thing that can be
    inspected later without re-running the call.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: object = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.code = code

    def as_payload(self) -> dict:
        """The shape the ledger records."""
        return {
            "message": str(self),
            "status_code": self.status_code,
            "error_code": self.code,
            "response_body": self.body,
        }


# The SDK raises a class per status; map back so the ledger records a number.
_STATUS_BY_EXC = {
    "BadRequestError": 400,
    "GatewayError": 502,
    "ServerError": 500,
    "SignatureVerificationError": 400,
}


def _wrap(exc: Exception, what: str) -> RazorpayError:
    """
    Turn an SDK exception into one carrying the provider's own words.

    The SDK puts the decoded error body in args[0]; when it is the documented
    {"error": {...}} shape the code is lifted out so it can be shown without
    the caller having to dig through the body.
    """
    body = exc.args[0] if exc.args else None
    code = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code") or err.get("reason")
    return RazorpayError(
        f"{what} failed: {exc}",
        status_code=_STATUS_BY_EXC.get(type(exc).__name__),
        body=body if body is not None else str(exc),
        code=code,
    )


class StubNotImplemented(Exception):
    """Raised when a live Razorpay call is attempted in stub mode."""
    pass


def _client() -> razorpay.Client:
    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )


def create_payment_link(
    amount_paise: int,
    description: str,
    session_id: str,
    reference_id: str | None = None,
    expire_by: int | None = None,   # unix timestamp
) -> dict:
    """
    Create a Razorpay Payment Link.

    Returns the full API response dict.
    Key fields used by the saga:
      - id      → payment_link_id (stored on SessionRecord)
      - short_url → URL shown to the human for manual payment in demo
    """
    if settings.STUB_MODE:
        raise StubNotImplemented("create_payment_link called in stub mode")

    payload: dict = {
        "amount": amount_paise,
        "currency": "INR",
        "description": description,
        "reference_id": reference_id or session_id,
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"session_id": session_id},
        "callback_url": "",          # caller sets this if needed
        "callback_method": "get",
    }
    if expire_by:
        payload["expire_by"] = expire_by

    try:
        response = _client().payment_link.create(payload)
    except Exception as exc:
        raise _wrap(exc, "create_payment_link") from exc
    fixtures.record("payment_link", response)
    return response


def fetch_payment_link(payment_link_id: str) -> dict:
    """
    Fetch a Payment Link's current state.

    This is what the poller reads: `status` moves to "paid" and `payments[]`
    gains the captured payment once the human has paid.
    """
    if settings.STUB_MODE:
        raise StubNotImplemented("fetch_payment_link called in stub mode")
    try:
        response = _client().payment_link.fetch(payment_link_id)
    except Exception as exc:
        raise _wrap(exc, f"fetch_payment_link({payment_link_id})") from exc
    fixtures.record("payment_link", response)
    return response


def fetch_payment(payment_id: str) -> dict:
    """Fetch a payment record by payment_id."""
    if settings.STUB_MODE:
        raise StubNotImplemented("fetch_payment called in stub mode")
    try:
        response = _client().payment.fetch(payment_id)
    except Exception as exc:
        raise _wrap(exc, f"fetch_payment({payment_id})") from exc
    fixtures.record("payment", response)
    return response


def create_order(amount_paise: int, receipt: str, notes: dict | None = None) -> dict:
    """Create a Razorpay Order (used in some checkout flows)."""
    if settings.STUB_MODE:
        raise StubNotImplemented("create_order called in stub mode")
    try:
        response = _client().order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
            "notes": notes or {},
        })
    except Exception as exc:
        raise _wrap(exc, "create_order") from exc
    fixtures.record("order", response)
    return response


def create_refund(payment_id: str, amount_paise: int, notes: dict | None = None) -> dict:
    """
    Initiate a full or partial refund on a captured payment.

    Issued over plain HTTP rather than through the SDK, deliberately. The SDK
    raises BadRequestError(description) and discards the rest of the envelope,
    so a rejection arrives as the bare string "invalid request sent" with the
    error code, metadata, reason, source and step all thrown away. Those fields
    are the only thing that could ever explain a rejection, and re-issuing the
    call afterwards to recover them would mean attempting the refund twice.

    One attempt, full response, whatever it says.
    """
    if settings.STUB_MODE:
        raise StubNotImplemented("create_refund called in stub mode")

    try:
        http = requests.post(
            f"{_API_BASE}/payments/{payment_id}/refund",
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            json={"amount": amount_paise, "notes": notes or {}},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        err = RazorpayError(
            f"create_refund({payment_id}) failed: {exc}",
            status_code=None,
            body=str(exc),
            code="network_error",
        )
        fixtures.record("refund_attempt", err.as_payload(), ok=False)
        raise err from exc

    try:
        body = http.json()
    except ValueError:
        body = http.text

    if http.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        description = (
            error.get("description") if isinstance(error, dict) else str(body)
        )
        err = RazorpayError(
            f"create_refund({payment_id}) failed: {description}",
            status_code=http.status_code,
            body=body,          # the whole envelope, exactly as returned
            code=(error or {}).get("code") if isinstance(error, dict) else None,
        )
        # A rejection is a real result and is recorded like any other. This is
        # the response the failure saga shows verbatim.
        fixtures.record(
            "refund_attempt", err.as_payload(),
            ok=False, status_code=http.status_code,
        )
        raise err

    fixtures.record("refund_attempt", body, ok=True, status_code=http.status_code)
    return body


def list_settlements(count: int = 10) -> dict:
    """
    What the account has actually settled.

    Used to establish whether a captured payment could possibly be refundable
    yet. The refund error body does not say, so this is where that fact comes
    from.
    """
    if settings.STUB_MODE:
        raise StubNotImplemented("list_settlements called in stub mode")
    try:
        return _client().settlement.all({"count": count})
    except Exception as exc:
        raise _wrap(exc, "list_settlements") from exc


def verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """
    Verify an incoming Razorpay webhook signature.
    Returns True if valid, False otherwise (never raises).
    """
    try:
        razorpay.Client(auth=("", "")).utility.verify_webhook_signature(
            body.decode(), signature, secret
        )
        return True
    except Exception:
        return False
