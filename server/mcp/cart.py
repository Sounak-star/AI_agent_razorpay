"""
Server-authoritative cart construction, and the lifecycle events around it.

This is the single place a Cart is built from SKU IDs, shared by the REST
checkout path, the eval harness, the seeder and the demo runner. Before it
existed each path built carts inline and none of them recorded what it had
done, which is why completed sessions carried only two ledger entries.

Three lifecycle events are emitted here because this is where the corresponding
work happens:

  CATALOG_QUERIED  each SKU is resolved against the catalog
  QUOTE_ISSUED     the server computes the authoritative total and persists a
                   QuoteRecord with a TTL, so quote_id refers to a real row
  CART_BUILT       the Cart is assembled from the priced line items

The payloads are deliberately complete enough that a reader can reconstruct
exactly what was bought and for how much from the ledger alone — no catalog
lookup, no session table, no other source. That is the acceptance bar: an audit
trail that needs a second source to interpret is not an audit trail.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from server.db.models import QuoteRecord
from server.ledger.chain import append
from server.ledger.events import EventType
from server.mandate.schema import Cart, CartItem, IntentMandate
from server.mcp.catalog import get_authoritative_price, get_sku_by_id

# Matches the TTL documented on QuoteRecord.
QUOTE_TTL_SECONDS = 300


class CartBuildError(ValueError):
    """A SKU could not be resolved against the catalog."""


def record_intent_signed(
    db: Session,
    session_id: str,
    intent: IntentMandate,
    *,
    extra: dict | None = None,
) -> None:
    """
    Record INTENT_SIGNED for a mandate the caller has just signed.

    Carries the mandate's own expiry, so the ledger shows not just what was
    authorised but for how long — a reader can tell whether a later action fell
    inside the window without fetching the JWT.
    """
    append(db, session_id, EventType.INTENT_SIGNED, {
        "jti": intent.jti,
        "buyer_id": intent.sub,
        "merchant_id": intent.aud,
        "budget_paise": intent.budget_paise,
        "categories": list(intent.categories),
        "max_line_items": intent.max_line_items,
        # Retained so payloads written before the rename still read.
        "max_items": intent.max_line_items,
        "estimate_paise": intent.estimate_paise,
        "exp": intent.exp,
        "expires_at": datetime.fromtimestamp(intent.exp, tz=timezone.utc).isoformat(),
        **(extra or {}),
    })


def record_catalog_queried(
    db: Session,
    session_id: str,
    *,
    query: str | None,
    filters: dict,
    sku_ids_returned: list[str],
    replayed_from_fixture: bool = False,
) -> None:
    """
    Record a catalog search: what was asked for, and which SKUs came back.

    IDs only, by design. Prices belong to QUOTE_ISSUED, which is the event that
    binds a price to a purchase; repeating them here would create a second
    place a price could appear to be set, and there must be exactly one.
    """
    append(db, session_id, EventType.CATALOG_QUERIED, {
        "query": query,
        "filters": filters,
        "sku_ids_returned": sku_ids_returned,
        "result_count": len(sku_ids_returned),
        "source": "server_catalog",
    }, replayed_from_fixture=replayed_from_fixture)


def record_no_cart_built(
    db: Session,
    session_id: str,
    *,
    reason: str,
    proposed_skus: list[str] | None = None,
    rationale: str | None = None,
) -> None:
    """
    Record that the agent finished without a usable cart.

    Carries what the model actually proposed and its stated rationale, plus the
    usage of the call that produced it, so the failure can be read without
    re-running anything. The model's own words are recorded as evidence of what
    it returned — they are not treated as an explanation of why the session
    stopped, which is decided here.
    """
    from server.db.models import LedgerEntry

    last_call = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.session_id == session_id,
            LedgerEntry.event_type == EventType.LLM_CALL.value,
        )
        .order_by(LedgerEntry.seq.desc())
        .first()
    )
    call = (last_call.payload or {}) if last_call else {}

    append(db, session_id, EventType.NO_CART_BUILT, {
        "reason": reason,
        "proposed_skus": list(proposed_skus or []),
        "agent_rationale": (rationale or "")[:400] or None,
        # Provenance of the call that produced nothing usable.
        "model": call.get("model"),
        "input_tokens": call.get("input_tokens"),
        "output_tokens": call.get("output_tokens"),
        "llm_call_seq": last_call.seq if last_call else None,
    })


def build_authoritative_cart(
    *,
    db: Session,
    session_id: str,
    sku_ids: list[str],
    quantities: list[int],
    merchant_id: str,
    replayed_from_fixture: bool = False,
) -> Cart:
    """
    Resolve SKUs against the catalog and build a priced Cart, recording the
    catalog lookup, the quote and the assembled cart in the ledger.

    Prices come from get_authoritative_price() only. A price supplied by a
    caller — or by a model — is never consulted, which is what makes the
    QUOTE_ISSUED entry meaningful.
    """
    if len(sku_ids) != len(quantities):
        raise CartBuildError("sku_ids and quantities must be the same length")

    # A cart with nothing in it has no total to authorise and no items to judge.
    # Refused at construction so it can never reach the policy engine.
    if not sku_ids:
        raise CartBuildError("cannot build a cart with no items")

    # ── Catalog lookup ────────────────────────────────────────────────────────
    items: list[CartItem] = []
    for sku_id, qty in zip(sku_ids, quantities):
        price = get_authoritative_price(sku_id)
        if price is None:
            raise CartBuildError(f"SKU '{sku_id}' not found in catalog")
        sku = get_sku_by_id(sku_id, include_internal=True) or {}
        items.append(CartItem(
            sku_id=sku_id,
            name=sku.get("name", sku_id),
            category=sku.get("category", "unknown"),
            quantity=qty,
            unit_price_paise=price,
        ))

    record_catalog_queried(
        db, session_id,
        query=None,
        filters={"sku_ids": list(sku_ids), "lookup": "by_id"},
        sku_ids_returned=[i.sku_id for i in items],
        replayed_from_fixture=replayed_from_fixture,
    )

    # ── Quote ─────────────────────────────────────────────────────────────────
    cart = Cart(merchant_id=merchant_id, items=items)

    # Line items are spelled out here — sku, name, quantity, unit price and
    # line total — because this is the entry that fixes the price. Everything
    # needed to re-add the numbers is present.
    line_items = [
        {
            "sku_id": i.sku_id,
            "name": i.name,
            "category": i.category,
            "quantity": i.quantity,
            "unit_price_paise": i.unit_price_paise,
            "line_total_paise": i.unit_price_paise * i.quantity,
        }
        for i in cart.items
    ]

    quote_id = str(uuid.uuid4())
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=QUOTE_TTL_SECONDS)

    # Persisted so quote_id names a real row with a real TTL rather than being
    # an identifier that refers to nothing.
    db.add(QuoteRecord(
        id=quote_id,
        session_id=session_id,
        sku_ids=list(sku_ids),
        quantities=list(quantities),
        items=line_items,
        total_paise=cart.total_paise,
        cart_hash=cart.canonical_hash(),
        expires_at=expires_at.replace(tzinfo=None),
    ))
    db.commit()

    append(db, session_id, EventType.QUOTE_ISSUED, {
        "quote_id": quote_id,
        "items": line_items,
        "total_paise": cart.total_paise,
        "currency": "INR",
        "expires_at": expires_at.isoformat(timespec="milliseconds"),
        "ttl_seconds": QUOTE_TTL_SECONDS,
        "priced_by": "server",
    }, replayed_from_fixture=replayed_from_fixture)

    # ── Cart ──────────────────────────────────────────────────────────────────
    append(db, session_id, EventType.CART_BUILT, {
        "cart_hash": cart.canonical_hash(),
        "quote_id": quote_id,
        "merchant_id": merchant_id,
        "items": line_items,
        "total_paise": cart.total_paise,
        "item_count": len(cart.items),
    }, replayed_from_fixture=replayed_from_fixture)

    return cart
