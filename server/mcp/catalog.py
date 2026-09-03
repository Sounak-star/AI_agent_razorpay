"""
Catalog loader and sanitizer.

sanitize(sku) strips all keys that start with '_' before any data
is handed to the MCP tools or the LLM. This is defence-in-depth:
even if an injection label leaked through, it would be stripped here.

The test asserts that no field starting with '_' ever appears in
search_products output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

_CATALOG_PATH = Path(__file__).parent.parent.parent / "seed" / "catalog.json"
_CATALOG: dict | None = None

# Delimiters that a product description might use to escape context.
# We wrap descriptions in a neutral sentinel so the LLM can see them
# as opaque data, not as instruction text.
_DELIMITER_START = "<<<PRODUCT_DESCRIPTION_START>>>"
_DELIMITER_END = "<<<PRODUCT_DESCRIPTION_END>>>"


def _load() -> dict:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return _CATALOG


def sanitize(sku: dict[str, Any]) -> dict[str, Any]:
    """
    Remove all keys starting with '_' from a SKU dict.

    This strips _has_injection, _injection_type, and any future internal
    markers before the data is serialised into the MCP response.
    The LLM never sees these fields.
    """
    return {k: v for k, v in sku.items() if not k.startswith("_")}


def wrap_description(description: str) -> str:
    """
    Wrap a product description in explicit delimiters.

    This is defence-in-depth: an injected instruction cannot masquerade
    as a system message because the LLM's system prompt explicitly tells
    it that everything between these delimiters is untrusted user data.
    The real security boundary is that the LLM cannot compute totals or
    set policy verdicts regardless of what it reads.
    """
    return f"{_DELIMITER_START}\n{description}\n{_DELIMITER_END}"


def get_all_skus(include_internal: bool = False) -> list[dict]:
    """
    Return all SKUs from the catalog.

    include_internal=True is only used by server-side logic (e.g. price
    lookup). The MCP surface always passes include_internal=False.
    """
    catalog = _load()
    skus = catalog.get("skus", [])
    if include_internal:
        return list(skus)
    return [sanitize(s) for s in skus]


def search_skus(
    query: str | None = None,
    category: str | None = None,
    max_price_paise: int | None = None,
    min_price_paise: int | None = None,
    limit: int = 10,
) -> list[dict]:
    """
    Search catalog with optional filters.

    Returns sanitized SKU dicts — no '_' keys, descriptions wrapped.
    Assertion: no key in any returned dict starts with '_'.
    """
    skus = get_all_skus(include_internal=True)  # need internal for filtering stock

    results = []
    for sku in skus:
        # Only purchasable items
        if not sku.get("agent_purchasable", False):
            continue
        # Stock filter
        if sku.get("stock", 0) <= 0:
            continue
        # Category filter
        if category and sku.get("category") != category:
            continue
        # Price filters
        price = sku.get("price_paise", 0)
        if max_price_paise is not None and price > max_price_paise:
            continue
        if min_price_paise is not None and price < min_price_paise:
            continue
        # Text search (case-insensitive substring in name or description)
        if query:
            q = query.lower()
            if q not in sku.get("name", "").lower() and q not in sku.get("description", "").lower():
                continue
        results.append(sku)

    # Truncate by taking a turn from each category rather than the first N in
    # file order.
    #
    # The catalogue grew by appending, so plain slicing returned only the
    # original SKUs and made every item added later invisible to an unfiltered
    # search — a request for one of them came back "no match" while it sat in
    # stock. Round-robin keeps every category represented no matter how the
    # file is ordered or which end new products are added to.
    if len(results) > limit:
        by_category: dict[str, list[dict]] = {}
        for sku in results:
            by_category.setdefault(sku.get("category", ""), []).append(sku)

        interleaved: list[dict] = []
        rank = 0
        while len(interleaved) < limit:
            took = False
            for bucket in by_category.values():
                if rank < len(bucket):
                    interleaved.append(bucket[rank])
                    took = True
                    if len(interleaved) == limit:
                        break
            if not took:
                break
            rank += 1
        results = interleaved

    # Sanitize + wrap descriptions
    clean = []
    for s in results:
        s2 = sanitize(s)
        s2["description"] = wrap_description(s2.get("description", ""))
        clean.append(s2)

    # Invariant: no '_' keys in any output dict
    for item in clean:
        assert not any(k.startswith("_") for k in item), (
            f"Internal field leaked into MCP output: {[k for k in item if k.startswith('_')]}"
        )

    return clean


def get_sku_by_id(sku_id: str, include_internal: bool = False) -> Optional[dict]:
    """
    Look up a single SKU by id.

    Server-side price resolution MUST use include_internal=True to get the
    authoritative price. The MCP tool uses include_internal=False.
    """
    catalog = _load()
    for sku in catalog.get("skus", []):
        if sku.get("id") == sku_id:
            if include_internal:
                return sku
            s2 = sanitize(sku)
            s2["description"] = wrap_description(s2.get("description", ""))
            return s2
    return None


def get_authoritative_price(sku_id: str) -> Optional[int]:
    """
    Return the server-authoritative price_paise for a SKU.

    This is the ONLY price source for cart construction. The LLM's output
    is never trusted for price.
    """
    sku = get_sku_by_id(sku_id, include_internal=True)
    if sku is None:
        return None
    return sku.get("price_paise")


def get_merchant_info() -> dict:
    catalog = _load()
    return {
        "merchant_id": catalog.get("merchant_id"),
        "merchant_name": catalog.get("merchant_name"),
        "catalog_version": catalog.get("catalog_version"),
        "sku_count": len(catalog.get("skus", [])),
        "categories": sorted({s.get("category") for s in catalog.get("skus", [])}),
    }
