"""
LLM call accounting.

Every live model call records one LLM_CALL ledger entry carrying the model id,
token usage as reported by the API, the priced cost, and wall-clock latency.

Why this lives in the ledger rather than a side table:
  The dashboard's "mean cost per session" and "mean latency" tiles must trace to
  recorded facts, not to client-side estimates. Putting the measurement in the
  append-only chain means those numbers are covered by the same integrity
  guarantee as every other claim the system makes.

Stub runs make no LLM calls, so they record no LLM_CALL rows. Metrics then
report zero samples rather than a fabricated average.
"""

from __future__ import annotations

import logging
from typing import Any

from server.config import settings
from server.ledger.chain import append
from server.ledger.events import EventType

log = logging.getLogger(__name__)

# USD per million tokens, as published for each model we call.
# Kept explicit so a price change is a visible diff, not a silent drift.
#
# Models not listed here are priced from LLM_PRICE_*_USD_PER_MTOK if those are
# set, and otherwise recorded as unpriced. No rate is ever guessed: a cost
# figure on the dashboard derived from a made-up rate would be exactly the kind
# of unverifiable number this system exists to eliminate.
_PRICING_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    #  model id                          (input, output)
    #
    # Empty on purpose. The only entry here used to be a Claude model this
    # build has never called — a rate that could only ever have priced a model
    # that was not running. The model actually in use is Groq
    # qwen/qwen3.8-27b, and no rate is hardcoded for it because none has been
    # verified. Set LLM_PRICE_INPUT_USD_PER_MTOK / LLM_PRICE_OUTPUT_USD_PER_MTOK
    # to price it; until then calls record cost_usd_micros=null and the
    # dashboard shows token counts rather than a fabricated figure.
}


def extract_token_usage(usage: Any) -> tuple[int, int]:
    """
    Pull (input, output) token counts off a usage object from either API style.

    Anthropic reports input_tokens / output_tokens; the OpenAI-compatible APIs
    that Groq and xAI expose report prompt_tokens / completion_tokens. Reading
    only the Anthropic names silently recorded every call as zero tokens after
    the switch to the OpenAI SDK, which in turn made every cost zero — a wrong
    number that looked like a real measurement.
    """
    if usage is None:
        return 0, 0

    def field(*names: str) -> int:
        for name in names:
            value = getattr(usage, name, None)
            if value is None and isinstance(usage, dict):
                value = usage.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    return (
        field("input_tokens", "prompt_tokens"),
        field("output_tokens", "completion_tokens"),
    )


def price_usd_micros(model: str, input_tokens: int, output_tokens: int) -> int | None:
    """
    Price a call in micro-USD (1e-6 USD). Returns None for an unpriced model —
    the caller records the usage anyway and metrics excludes it from the mean.
    """
    rates = _PRICING_USD_PER_MTOK.get(model)

    if rates is None:
        # Configured rate for whatever model this deployment is using. Both
        # halves must be set, so a half-filled config cannot produce a
        # confidently wrong number.
        rate_in = settings.LLM_PRICE_INPUT_USD_PER_MTOK
        rate_out = settings.LLM_PRICE_OUTPUT_USD_PER_MTOK
        if rate_in <= 0 and rate_out <= 0:
            return None
        rates = (rate_in, rate_out)

    rate_in, rate_out = rates
    usd = (input_tokens / 1_000_000) * rate_in + (output_tokens / 1_000_000) * rate_out
    return round(usd * 1_000_000)


def record_llm_call(
    *,
    session_id: str,
    model: str,
    usage: Any,
    latency_ms: int,
    purpose: str,
    db=None,
) -> None:
    """
    Append one LLM_CALL entry. Never raises — instrumentation must not be able
    to fail a payment flow.

    `usage` is the SDK usage object (or any object exposing input_tokens /
    output_tokens). `db` may be omitted, in which case a short-lived session is
    opened and closed here.
    """
    owns_db = db is None
    try:
        input_tokens, output_tokens = extract_token_usage(usage)
        cost_micros = price_usd_micros(model, input_tokens, output_tokens)

        if owns_db:
            from server.db.session import SessionLocal
            db = SessionLocal()

        append(db, session_id, EventType.LLM_CALL, {
            "model": model,
            "purpose": purpose,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd_micros": cost_micros,
            "priced": cost_micros is not None,
            "latency_ms": latency_ms,
        })
    except Exception as exc:                      # noqa: BLE001 — see docstring
        log.warning("[llm_cost] failed to record LLM_CALL: %s", exc)
    finally:
        if owns_db and db is not None:
            try:
                db.close()
            except Exception:                     # noqa: BLE001
                pass
