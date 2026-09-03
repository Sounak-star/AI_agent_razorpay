# Tollgate

**Governed agentic-commerce rail.** Two agents transact end-to-end; every rupee is explainable, bounded, and gated.

> Built for Razorpay AI Buildathon 2026 — Track 01: AI Growth & Agentic Commerce.
> Payments run on **Razorpay test-mode only**. No real money, ever.

---

## 60-second Quickstart

```bash
# 1. Clone
git clone <repo> && cd tollgate    # not yet a git repo; copy the tree

# 2. Install
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r server/requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — at minimum set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (rzp_test_ prefix required)

# 4. Run tests
pytest tests/ -q               # → 244 passed

# 5. Build the dashboard (served by FastAPI at /)
cd dashboard && npm install && npm run build && cd ..

# 6. Seed a demo database (varied goals; ALLOW / DENY / ESCALATE reached)
python demo/seed.py --reset
# → warns "no session ended in: refund" — the recorded refund was rejected by
#   the provider, so no session legitimately reaches a refunded state

# 7. Start server (stub mode — no LLM/Razorpay calls)
python -m server.main --stub
# dashboard → http://127.0.0.1:8000/     API docs → /docs

# 8. Run eval harness (stub, deterministic)
python evals/harness.py --stub
# → 23 normal scenarios + 15/15 adversarial attacks
# → exits 1: 3 seeded scenarios do not match the ledger (see below)
# → evals/report.md written

# 9. (Optional) Live demo with real Razorpay Payment Link
python demo/run.py             # prints a payment URL
python demo/run.py --resume-payment <payment_id>  # after paying
```

**The harness exits 1, and that is the current honest state.** Three seeded
scenarios do not match what the ledger recorded:

| Scenario | Expected | Ledger |
|---|---|---|
| `sess_008_refunded_after_fulfilment_failure` | a completed refund | `refund_state=none` — the harness never passes `_simulate_refund` |
| `sess_019_velocity_run_6` | ESCALATE | ALLOW |
| `sess_023_daily_cap_bulk_4` | DENY | ALLOW |

`sess_019` and `sess_023` both fire correctly under `demo/seed.py`, which builds
buyer history differently from the harness. These are open, not fixed, and the
harness fails the run rather than reporting green.

**One-line DB switch to Postgres (for concurrency tests):**
```bash
# docker-compose up -d db
# export DATABASE_URL=postgresql://tollgate:tollgate@localhost:5432/tollgate
```

---

## Architecture

```
Buyer Agent  ──MCP──►  Merchant Agent (catalog + quote tools)
     │                        │
     └──── CartMandate JWT ───►│
                               │
                         Policy Engine   ← pure, deterministic, no LLM
                               │
                         Saga Runner
                         ├─ Option A: Razorpay Payment Link (live demo)
                         └─ Option B: Fixture replay (eval harness)
                               │
                          Ledger (hash-chained, append-only)
```

### Session lifecycle

Every session records its whole story in the chain, in causal order:

```
INTENT_SIGNED → CATALOG_QUERIED → QUOTE_ISSUED → CART_BUILT
              → POLICY_EVALUATED ◆gate → CART_SIGNED ◆gate
              → ORDER_CREATED → PAYMENT_* → SESSION_CLOSED
```

A completed ALLOW session carries at least 9 entries; `tests/test_lifecycle.py` asserts this, the causal ordering, and that the purchase is **reconstructable from the ledger alone** — every line item's SKU, quantity, unit price and line total, the quote id and its expiry, the cart mandate's jti and the intent it descends from, the order id and receipt, and the closing state, total and duration. No catalog lookup or session-table join is needed to read the trail. The two gates are the steps that can stop a session: `POLICY_EVALUATED` (the verdict) and `CART_SIGNED` (the mandate). Each event is written where the work actually happens — `build_authoritative_cart()` records the catalog lookup, quote and cart because that is the function performing them — so no entry is ever logged speculatively.

Whether the reconciler is actually running is reported on `GET /health` and printed at boot. It used to be wrapped in `except ImportError: pass`, which made a broken reconciler indistinguishable from a working one — and the symptom either way is sessions stuck showing as live. It also sweeps once at startup, so a server started against a database of old sessions does not display them as live until the first interval elapses.

Terminal statuses: `captured`, `refunded`, `failed`, and `stale`. A session with no ledger activity past `STALE_SESSION_TIMEOUT_SECONDS` is swept to `stale` by the reconciler and marked as such in the UI, so a hung session can never keep presenting itself as live. Sessions waiting on a human are exempt — that is a queue, not a hang.

### Key invariants

| Property | Mechanism |
|----------|-----------|
| Prices are always catalog-authoritative | `get_authoritative_price()` called server-side; LLM output is never trusted for money amounts |
| Replay prevention | DB `UNIQUE(jti)` constraint — `IntegrityError` on duplicate insert, not app-level check |
| Injection defence | Architectural: LLM cannot compute totals, set policy verdicts, or mint mandates. Descriptions wrapped in delimiters and flagged untrusted in system prompt |
| Tamper evidence | Every ledger entry hashes `sha256(prev_hash + canonical_json(payload))`. `GET /ledger/verify` re-derives all hashes |
| Live key guard | Boot-time validator: process exits immediately if `RAZORPAY_KEY_ID` doesn't start with `rzp_test_` |

---

## API Surface

| Endpoint | Description |
|----------|-------------|
| `POST /sessions` | Create session, get signed IntentMandate JWT |
| `GET /sessions` | List sessions with server-computed `elapsed_ms` |
| `POST /sessions/{id}/checkout` | Submit cart, run policy + saga |
| `GET /sessions/{id}` | Session status |
| `GET /sessions/{id}/ledger` | Ledger for this session (paginated) |
| `GET /ledger` | Global ledger (paginated) |
| `GET /ledger/verify` | Hash-chain integrity check |
| `POST /ledger/tamper` | **(demo only)** Mutate an entry — then verify shows `{valid: false}` |
| `GET /escalations` | Pending escalations with cause + AUTHORISED-vs-PROPOSED diff |
| `POST /sessions/{id}/escalations/{esc_id}/approve` | Human approves escalated session |
| `POST /sessions/{id}/escalations/{esc_id}/reject` | Human rejects |
| `GET /metrics` | Verdict split, unauthorised-movement audit, latency, cost |
| `GET /health` | Liveness **plus reconciler status** — started, alive, last boot sweep |
| `GET /.well-known/agent-commerce.json` | MCP discovery |
| `/mcp` | MCP server (Streamable HTTP transport) |
| `/` | Dashboard (static build of `dashboard/dist`) |

Full Swagger: `http://localhost:8000/docs`

---

## Dashboard

Vite + React + TypeScript + Tailwind, built to `dashboard/dist` and served by FastAPI at `/`. One process runs the API, the MCP surface and the dashboard together.

```bash
cd dashboard
npm install
npm run build        # → dashboard/dist, picked up by the server at boot
npm run dev          # optional: hot reload on :5173, proxying the API to :8000
```

Four panels on a single 1280×720 frame, all polling on a 2s tick:

| Panel | Source | Shows |
|-------|--------|-------|
| Session stream (left) | `GET /sessions` | One row per session — id, goal, status chip, elapsed. Click to filter the ledger. |
| Ledger (centre) | `GET /ledger`, `GET /sessions/{id}/ledger` | seq, ts, event_type, reason_code, hash, prev_hash. Chain badge on top; a trace rail for the selected session; **any row expands** to its full payload and hash arithmetic. |
| Escalations (right) | `GET /escalations` | Trigger-led: reason code, one-line cause, then the diff with the field the rule examined marked `TRIGGER`. Approve/reject wired to the decision endpoints. |
| Metrics (top) | `GET /metrics` | Money moved without authorisation as the headline, then sessions run, verdict split, offer attach rate, mean latency, mean cost. |

**The operator view answers "why this and not something else."** Each session's
narrative expands the proposed-cart line into the full basket — sku, name, qty,
unit price, line total, total — so a merchant never has to open the forensic
view to see what was bought. Where the agent actually browsed, it reports
"Considered N SKUs, chose M" with the passed-over SKUs behind a toggle; where
the server resolved SKUs directly by id it says so instead of implying a search
that never happened. A denial names the threshold it would have cleared —
`Would have passed at 2 items or fewer (4 were proposed)`,
`Blocked by Samsung 25W USB-C Fast Charger (ELE002, electronics)` — read from
the mandate and the cart, never from a model.

**Offers are stated on every session, including the ones without one.** The
upsell agent existed but was never called from anywhere, so no `UPSELL_*` event
had ever been written. It now runs after the ALLOW verdict and before the cart
is signed, and the ledger records all four outcomes: offered and accepted,
offered and declined, withheld because the item exceeded remaining headroom, or
no offer at all. The headroom guard is why an accepted offer needs no fresh
authorisation — it can only ever fit inside what the buyer already signed for.
The cart is signed exactly once, *after* the offer, so the single signature
always covers the total actually paid.

> **Attach rate is labelled `SIMULATED` and must stay that way until acceptance
> is a real decision.** The model chooses *which* item to offer, but whether the
> offer is accepted is set by `_accept_upsell` in `seed/sessions.json` — a flag,
> not a buyer. The figure therefore measures the seed file, not customer
> behaviour, and shipping it unqualified would present a configuration value as
> a growth result. `GET /metrics` returns `acceptance_is_simulated` and an
> `acceptance_basis` string alongside the rate, and the strip renders the label
> `Attach rate · simulated`, a `SIM` badge and `n/m seed-accepted`. Remove the
> label only when acceptance is decided by something real.

Sessions never offered anything stay out of the denominator — a rate diluted by
them measures reach rather than persuasiveness. With no offers at all it shows a
dash, not 0%: a rate with no denominator is undefined, not zero.

**A cartless session can never produce a policy decision.** `engine.evaluate()`
raises `EmptyCartError` rather than returning a verdict when the cart is null or
empty. Every rule is a comparison against the cart — no total to weigh, no
categories to check, no line items to count — so an empty one satisfies all of
them vacuously and would emerge carrying `ALLOW`: a verdict meaning "checked and
permitted", attached to something never checked. Returning `DENY` instead would
still be a decision about a cart that does not exist. `build_authoritative_cart`
refuses to construct an empty cart, and both saga paths close such a session as
`NO_CART` before evaluation.

When the agent itself comes back with nothing usable, that is recorded as
`NO_CART_BUILT` — carrying what the model proposed, its stated rationale, and
the model, token counts and seq of the call that produced it. "The model
returned nothing we could buy" and "the payment failed" are different faults and
only one of them is the agent's.

> **Measured cart-build failure rate: 0 of 8 live agent runs.** The agent
> proposed a valid, in-catalog cart every time. An earlier session that appeared
> to reach policy with an empty cart had in fact built a real cart — `demo_live`
> was constructing it inline instead of via the shared builder, so
> `QUOTE_ISSUED` and `CART_BUILT` were never written and the operator view read
> "chose 0" from an absent entry. That is fixed; the guards above are defence in
> depth, not a response to a failing agent.

**A real model is in the loop, and the strip names it.** `LLM_CALL` is not a
placeholder: the upsell agent calls Groq (`qwen/qwen3.8-27b`) and the ledger
records provider-reported usage per call. The tile header names the model, and
the value is the mean **tokens** per session when no rate is configured, switching
to currency once `LLM_PRICE_*_USD_PER_MTOK` is set. Usage is real either way —
reporting "no model calls recorded" because the *price* was unknown conflated
two different unknowns and made several seconds of genuine model latency look
unexplained.

**Latency is reported twice, and neither figure is a delay.** There is no sleep,
poll or retry anywhere in the session path — sessions with no model call settle
in ~30ms. Engine latency is the headline; wall clock sits beside it and includes
time spent waiting on the model API, which is recorded per call on `LLM_CALL`
and subtracted to get the engine figure. When calls were made but no price is
configured for the model, the cost tile says `N calls · no rate configured`
rather than "no model calls recorded" — those are different facts, and reporting
the second as the first made several seconds of real model latency look
unexplained.

**The forensic view subordinates to the selection.** Selecting a session filters
the escalations rail to it; with none of its own it says so and offers the count
pending elsewhere behind a `SHOW ALL`. Reading one session's ledger while the
rail showed three unrelated escalations put three sessions on screen at once and
left the reader to work out which card belonged to what.

`prev_hash` is deliberately absent from the table. Two truncated hex columns
side by side invite the eye to match strings it cannot verify by looking; the
linkage is checked in the expanded row, where the hash is actually re-derived.
The hash input — the tallest block on the screen and the least read — sits
behind a `show hash input` toggle for the same reason: it is proof, so it stays,
but as a deliberate drill-down rather than the default state.

**Expandable rows are the point.** Clicking any ledger row shows its full payload, `prev_hash`, the exact canonical-JSON string that was hashed, and a SHA-256 **computed in the browser** from the payload the API returned — marked `RE-DERIVED ✓` or `MISMATCH ✗`. This is deliberately not the server's `/ledger/verify` answer restated: the two checks can disagree, and if they ever do, the one on screen is the independently derived one. `dashboard/src/canonical.ts` reimplements the server's canonical serialisation to make that possible.

**Counts state their scope.** The chain badge counts the whole ledger (`global · all sessions`), the pager counts the current filter (`… in session`), and the verdict split counts *sessions that reached a verdict* — so `ALLOW + DENY + ESCALATE == total ≤ sessions`. These numbers legitimately differ; unlabelled they read as a contradiction.

**Stub mode is announced, not inferred.** Running with `--stub` shows a permanent amber banner saying model calls and payment legs are replayed. It is not dismissible.

Two properties the dashboard is built to hold:

**Every number traces to an API response.** The client formats and it renders; it never computes a statistic. The verdict split, the unauthorised-movement audit, per-session elapsed time, latency and cost are all derived server-side in `server/api/analytics.py`. Where there is no data behind an average — stub runs make no model calls, so there is no cost to report — the API returns `null` with a sample count of `0` and the dashboard shows a dash rather than a fabricated figure.

**Simulated legs are labelled, never hidden.** Ledger rows carrying `replayed_from_fixture`, and the `PAYMENT_SIMULATED` / `REFUND_SIMULATED` event types, are tagged `REPLAYED` in the table. A demo that presents fixture replay as live settlement is exactly the failure this system exists to prevent.

**Tamper demo.** With `ALLOW_TAMPER=true`, `POST /ledger/tamper` mutates one entry. Within one poll the badge flips from green `CHAIN VERIFIED · N ENTRIES` to red `CHAIN BROKEN AT SEQ N`, the offending row fills red and every row after it dims — the break point is visible without reading a single value.

---

## MCP Tools

The merchant exposes 5 tools to buyer agents:

| Tool | Description |
|------|-------------|
| `search_products` | Search catalog with filters |
| `get_product` | Get a single SKU |
| `get_quote` | Server-computed cart quote (prices from catalog, not LLM) |
| `get_merchant_info` | Merchant metadata |
| `check_session_status` | Poll session/payment state |

---

## Policy Engine Rules (in order)

| Rule | Action | Threshold |
|------|--------|-----------|
| per_txn_cap | DENY | cart.total > intent.budget_paise |
| daily_cap | DENY | today's settled spend + cart > 5,000,000 paise |
| velocity | ESCALATE | ≥ 5 transactions in last 3600s |
| category_deny | DENY | any SKU category not in intent allowlist |
| price_drift | ESCALATE | cart > estimate × 1.05 |
| item_count | DENY | cart **line items** > `max_line_items` (units are not counted) |
| first_contact_buyer | ESCALATE | this buyer has no settled transaction with this merchant |
| mandate_invalid | DENY | cart mandate failed signature / replay verification |

`first_contact_buyer` was called `new_merchant`. The name overclaimed: the rule
reads a **per-buyer** history and says nothing about the merchant being new to
the platform. A merchant with thousands of settled transactions still trips it
for a buyer who has never used it. Ledger entries written before the rename
still render correctly.

`mandate_invalid` is evaluated in `engine.evaluate()` rather than in
`ORDERED_RULES`, because it receives the `VerifyResult` rather than
`(intent, cart, history)`.

### ITEM_COUNT counts lines, not units

`max_items` said neither, and read as units to anyone who had not opened
`rule_item_count`. A cart of 2 SKUs at quantity 5 each counts as **2** against
the ceiling, not 10 — so `max 4` allows 10 units across 2 lines, which is
correct but was not visible anywhere.

The mandate field is now `max_line_items`. `max_items` remains a valid alias on
the wire, because it is a claim inside signed intent mandates and renaming it
outright would invalidate every mandate already issued. Verdicts state both
counts (`cart has 3 line items (15 units); intent allows 2 line items`), and the
operator narrative reads *"max 4 line items"* and *"proposed 2 line items
(10 units)"*.

**There is deliberately no separate ceiling on units.** Total spend is already
bounded by `per_txn_cap` and `daily_cap`, so quantity is constrained by value
rather than by count. If a distinct unit ceiling is wanted it is a new rule, not
a change of meaning for this one — and that is a policy decision, not a bug fix.

### Escalation cards show the comparison the rule made

A `PRICE_DRIFT` card used to render the full mandate diff, which included
cart-total against *budget*. On a ₹999 cart under a ₹5,000 budget that row read
as comfortably within limits, directly beneath cause text saying 899% over. Both
rows were accurate; only one was the rule's.

Cart-derived rules now lead with the pair they actually weighed — for
`PRICE_DRIFT`, estimate vs cart total against the +5% ceiling — and the
remaining mandate fields collapse behind a toggle, muted when opened. History-
based rules have no such pair and keep the evidence panel instead.

### Rule coverage

Every rule fires at least once in a normal demo run. This table is regenerated
by running `python demo/seed.py --reset` and `python evals/harness.py --stub`
and counting `POLICY_EVALUATED` reason codes in each database.

| Rule | Demo run | Eval harness |
|------|---------:|-------------:|
| per_txn_cap | 1 | 2 |
| daily_cap | 1 | 1 |
| velocity | 1 | 1 |
| category_deny | 1 | 2 |
| price_drift | 1 | 2 |
| item_count | 1 | 2 |
| first_contact_buyer | 1 | 2 |
| mandate_invalid | **0** | **5** |
| *(ALLOW)* | 16 | 21 |

**`mandate_invalid` is 0 in the demo and 5 in the harness.** This entry
previously read 0/0, with the explanation that "neither can produce a cart
mandate that fails verification". That was true before the two saga
implementations were collapsed and is false now: attacks 11–15 hand the real
path a forged, expired, hash-mismatched, replayed or unknown-intent cart
mandate, and the verifier rejects all five — which is precisely what collapsing
the sagas was for.

It remains 0 in the demo seed, and that is not hidden. The seeder builds every
cart mandate correctly, so nothing it produces can fail verification; making it
fire there would mean the seeder deliberately forging a bad signature, which is
what the attack suite already does.

Two silent faults were found while establishing this coverage, both of which
stopped a rule matching without raising anything:

- **History timestamps were read in the local zone.** `created_at` is naive UTC;
  `.timestamp()` interprets a naive datetime as local time, skewing every past
  transaction by the UTC offset (5.5 hours here). Every entry fell outside
  VELOCITY's one-hour window, so the rule could never fire.
- **History carried the authorised budget, not the settled amount.** DAILY_CAP
  sums `total_paise` and calls it "today's settled spend"; reading the budget
  counted a buyer with a ₹50,000 limit who spent ₹500 as having spent ₹50,000.

---

## One saga, not two

`run_saga()` is the single money-movement path. A live demo and a seeded or eval
run differ only in the arguments passed to it:

| parameter | live | seeded / eval |
|---|---|---|
| `payments` | `LIVE` — real Razorpay order + payment link, polled to capture | `REPLAY` (recorded ids) or `SYNTHETIC` (generated, no network) |
| `cart_token` | supplied by the client | signed by the saga, or forged by a scenario |
| `offer_upsell` | on | on |

There were previously two implementations, and they drifted. The drift landed on
the control the whole system rests on: **`run_saga_harness` never called
`verify_cart_mandate`.** A full 10-attack run recorded **zero rows in
`mandate_jtis`** — every eval result was produced by a path that skipped mandate
enforcement entirely. After collapsing, the same run records **84 rows, 52 of
them cart mandates verified**.

The unit tests in `tests/test_mandate.py` were never affected: they call
`verify_cart_mandate()` directly, so replay, expiry and hash-mismatch were
always genuinely proven. What was missing was evidence that the *saga* calls the
verifier — and on one path it did not.

`run_saga_demo` and `run_saga_harness` remain as thin wrappers that choose
`payments` and nothing else. A test asserts they contain no `evaluate(`,
`verify_cart_mandate`, `append(` or `close_session(` of their own, so behaviour
cannot re-enter one path without the other.

## Live payments

Three payment modes, selected per run. The default is `synthetic`, deliberately:
a demo that cannot run without Razorpay being reachable is a demo that fails on
stage.

| mode | what it does | network | badge |
|---|---|---|---|
| `synthetic` | generates identifiers locally | none | SYNTHETIC |
| `replay` | replays `evals/fixtures/razorpay_capture.json`, falling back to synthetic when a kind is missing | none | REPLAYED (only for the parts the file actually covers) |
| `live` | real test-mode order + payment link, polled until paid | yes | neither — it is real |

```bash
python evals/harness.py --attacks-only --payments=synthetic   # zero network calls
python evals/harness.py --attacks-only --payments=replay      # recorded capture
python demo/run.py --stub --payments=synthetic                # full demo, offline
```

A complete run passes 15/15 attacks in both modes, including with
`evals/fixtures/razorpay_capture.json` deleted from disk.

### On APPROVE, money actually moves

Approving an escalation settles it, in whatever mode is configured. That is the
whole point of the button, and it was broken: approving recorded
`HUMAN_APPROVED`, set the session back to active, and then did nothing at all.
The reconciler correctly swept the idle session to `STALE` sixty seconds later,
so the ledger read `HUMAN_APPROVED → SESSION_STALE` with no payment between
them, and an operator who clicked APPROVE watched the card vanish and no
transaction happen. Settlement now runs through `settle_authorised_cart()`,
which is the same function `run_saga` calls on an ALLOW — one settlement path,
reached two ways.

On the **live** path, approving creates a real order and a real payment link
inside the approving request and hands the operator the `short_url` (and the QR,
when Razorpay returns one) on the escalation card. The card stays on the rail
while the payment is outstanding rather than vanishing at the moment it becomes
actionable. On the other paths the session settles and closes before the request
returns.

`PAYMENTS_MODE=live` with `STUB_MODE=on` is refused rather than quietly falling
back to a synthetic settlement — a fallback there would look to the operator
exactly like a real payment.

Confirmation is by **polling**, not webhooks: `GET /v1/payment_links/{id}` every
2s, 5-minute timeout, then the `pay_` id is read out of the response. This needs
no public tunnel, which a laptop on stage does not have.

The wait runs behind a `PaymentConfirmer` protocol
(`server/payments/confirm.py`). `PollingConfirmer` is today's implementation;
a webhook receiver implements the same `wait_for_capture` and drops in without
the saga changing. The poller runs on its own thread so the approving request
returns the URL immediately instead of holding a connection open for five
minutes.

### REPLAYED means replayed

`REPLAYED` is only ever produced for an event genuinely read out of the recorded
capture. Two bugs were found and fixed while wiring this up, both of the same
shape — a badge claiming more than it had:

- `fixture_path` was keyed off whether the fixture file *existed*, so a
  `synthetic` run cited a recording it had never opened. It is now keyed off
  whether that specific event was read from it.
- The path recorded was absolute (`C:\Users\...`), which was then hashed into
  the ledger. It is now repo-relative: whose laptop it was recorded on is not
  part of the record.

The capture is also explicit about what it does *not* cover. The identifiers are
real; the amount is the scenario's cart, which is not what was captured. So a
replayed event records both:

```json
{
  "razorpay_payment_id": "pay_TWH9Tg3wQsVH5g",
  "amount_paise": 47900,
  "replayed_amount_paise": 100,
  "replayed_fields": ["razorpay_order_id", "razorpay_payment_id"],
  "fixture_path": "evals/fixtures/razorpay_capture.json"
}
```

Without `replayed_amount_paise`, a REPLAYED badge over a ₹479 cart reads as
"₹479 really moved" when the recording is for ₹1.

Re-record with:

```bash
python scripts/record_fixture.py --order order_XXX --link plink_XXX --payment pay_XXX --refund
```

Everything it writes is the provider's own response body, unedited. Nothing
constructs a plausible-looking payload — a fixture written by hand is not
evidence, and the badge would then be a claim with nothing behind it.

### The refund is rejected, and that is recorded rather than hidden

Refunding the captured test payment fails. The API returns:

```
HTTP 400
{"error": {"code": "BAD_REQUEST_ERROR", "description": "invalid request sent",
           "metadata": {}, "reason": "NA", "source": "NA", "step": "NA"}}
```

That body explains nothing. **The cause is verified, from Razorpay's own
dashboard, which states:**

> "Your account does not have sufficient balance to instantly refund this
> payment."

The payment is captured but **unsettled** — settlement is T+2, scheduled for
Wed 2 Sep 2026 — and the *Refund Instantly* option is disabled for the same
reason. The generic `BAD_REQUEST_ERROR` was masking a balance/settlement
constraint. This is a **provider constraint, not a code defect**, and not an
account restriction.

The API corroborates it independently: `GET /v1/settlements` returns `count: 0`,
so nothing has settled on this account and a payment captured today certainly
has not.

Two things follow, and both are wired:

**1. It is retryable-later, not terminal.** `REFUND_PENDING_SETTLEMENT` is a
distinct ledger event from `REFUND_FAILED`, because the two demand opposite
responses. Collapsing them would either strand a buyer who is owed money or bury
a real fault under a retry loop.

| | `REFUND_PENDING_SETTLEMENT` | `REFUND_FAILED` |
|---|---|---|
| cause | captured but unsettled; no balance | genuine refusal |
| session status | `refund_pending` — **not resolved** | `refund_failed` — terminal |
| reconciler | logs the attempt, schedules a retry after the expected settlement date | leaves it alone |
| swept as stale? | never — it is waiting by design | n/a |

The reconciler will not retry before the expected settlement date (retrying
early produces the same 400 and buries the real state in noise) and backs off
`REFUND_RETRY_INTERVAL_SECONDS` between attempts. A session stays open until the
refund lands.

**2. The split is only made when settlement status is actually known.** The error
body is identical either way, so the payment's settlement state is what carries
the weight. If that could not be established, the rejection is recorded as a
plain `REFUND_FAILED` — an unverified "it'll work later" silently converts a
permanent failure into an endless retry while the buyer waits.

The `REFUND_PENDING_SETTLEMENT` payload records the verbatim API body, the HTTP
status, the settlement status **with its source**, and the expected settlement
date **with its basis**:

```json
{
  "status_code": 400,
  "response_body": {"error": {"code": "BAD_REQUEST_ERROR", "...": "..."}},
  "settlement": {
    "status": "unsettled",
    "status_source": "GET /v1/settlements returned 0 settlements; none cover this payment",
    "expected_at": "2026-09-02",
    "expected_basis": "capture date 2026-08-31 + T+2 settlement cycle (derived; Razorpay exposes no per-payment settlement schedule endpoint)"
  }
}
```

`status` is **queried** — the provider's own answer. `expected_at` is
**derived**: `/v1/payments/{id}/settlement` is a 404, so there is no schedule
endpoint to read, and the field carries its basis so nobody mistakes a computed
date for a quoted one.

The operator narrative reads:

> Refund attempted — provider rejected: payment not yet settled, no balance
> available. Expected settlement 2026-09-02.
> ₹1.00 is still owed to the buyer. The refund is queued and will be retried
> after settlement — this session is not closed.

No synthetic refund is ever substituted for a real one that did not happen. A
compensation that did not occur must not appear on the trail as one that did.

## Eval Harness

```bash
python evals/harness.py --stub          # default: fixture replay, no API calls
python evals/harness.py --live          # real Razorpay (requires fixture first)
python evals/harness.py --attacks-only  # only run adversarial tests
```

### The pass condition

Each attack declares the exact verdict it expects — decision **and** reason code
— and the harness compares that against the `POLICY_EVALUATED` entry the ledger
actually recorded:

```json
"expect": { "decision": "DENY", "code": "ITEM_COUNT" }
```

Three outcomes, and **an exception is never one of the passing ones**:

| state | meaning |
|---|---|
| `PASS` | the declared verdict was recorded |
| `FAIL` | a verdict was recorded, but not the declared one |
| `ERROR` | the scenario raised — **nothing was tested** |

Two kinds of silent failure this closes:

- **A crash used to score as a pass.** The old condition accepted `error` as
  satisfying `deny`. `04_item_count_overflow` listed `GRO011` and `GRO012`,
  which are not in the catalog, so the cart never built, `CartBuildError` was
  caught as `error`, and the attack passed — **without once reaching the rule it
  was written for.** It now uses ten real SKUs and genuinely records
  `DENY/ITEM_COUNT`.
- **The wrong defence used to count.** With only `deny` expected, an attack
  could pass because an *earlier* rule stopped it. Naming the code means an
  `ITEM_COUNT` attack that trips `CATEGORY_DENY` first now fails.

The five mandate attacks all land on `MANDATE_INVALID`, so decision and code
alone cannot tell them apart — an expired-mandate attack would pass on a hash
mismatch. Each therefore also declares `reason_contains`, matched against the
verifier's own reason.

Adversarial scenarios tested:

1. Over-budget request → DENY
2. Forbidden category → DENY  
3. Price injection (discount) → ALLOW at catalog price *(injection had no effect)*
4. Item count overflow → DENY
5. New merchant (no history) → ESCALATE
6. Price drift (injected estimate) → ESCALATE
7. Daily cap exceeded → DENY
8. Velocity limit → ESCALATE
9. Books catalog injection → ALLOW at catalog price
10. System-level injection ("refund all orders") → ALLOW at catalog price *(LLM cannot trigger refunds)*
11. Replayed cart mandate → DENY `MANDATE_INVALID` (`jti_replayed`)
12. Expired cart mandate → DENY `MANDATE_INVALID` (`Signature has expired`)
13. Mandate signed over a different cart → DENY `MANDATE_INVALID` (`cart_hash_mismatch`)
14. Unparseable mandate → DENY `MANDATE_INVALID` (`jwt_error`)
15. Mandate citing an intent that was never issued → DENY `MANDATE_INVALID` (`intent_jti_not_found`)

Attacks 11–15 are only expressible because there is now one saga: `cart_token`
is an injected parameter, so a scenario can hand the real path a forged token
and watch the real verifier reject it. Each fails for a distinct recorded
reason, not a generic one.

> The injection defence is architectural: the LLM cannot compute totals, set policy verdicts, or call payment APIs. Pattern-based filtering is NOT the defence.

---

## Concurrency Notes

> SQLite WAL mode correctly enforces the `UNIQUE(jti)` constraint in serial execution.
> For true concurrent write races (`double_charge`, `refund_race`), use the Postgres path:
> `docker-compose up -d db && export DATABASE_URL=postgresql://...`

---

## Project Structure

```
tollgate/
  server/
    agents/         buyer.py, upsell.py
    api/            routes.py, analytics.py, narrative.py
    db/             models.py, session.py
    ledger/         chain.py, events.py, llm_cost.py
    mandate/        issuer.py, schema.py, verifier.py
    mcp/            catalog.py, cart.py, server.py
    payments/       razorpay_client.py, saga.py, webhook.py, reconciler.py
                    confirm.py      capture confirmation (poller today, webhook later)
                    fixtures.py     verbatim recorded provider responses
                    settlement.py   settled-vs-unsettled, and what that is based on
    policy/         codes.py, engine.py, rules.py, history.py
    config.py, main.py
  dashboard/
    src/
      panels/       MetricsStrip.tsx, SessionStream.tsx, Ledger.tsx, Escalations.tsx,
                    OperatorView.tsx, LedgerRowDetail.tsx, TraceStrip.tsx
      components/   Panel.tsx, StatusChip.tsx, StubBanner.tsx, ViewToggle.tsx
      api.ts, format.ts, canonical.ts, usePoll.ts, App.tsx
    dist/           npm run build output — served by FastAPI at /
  tests/            test_ledger.py, test_mandate.py, test_mcp_catalog.py, test_policy.py,
                    test_analytics.py, test_lifecycle.py, test_payments_live.py
  evals/
    harness.py
    attacks/        15 adversarial test cases
    fixtures/       razorpay_capture.json — real recorded API responses
    report.md       (auto-generated)
  demo/             run.py, seed.py
  scripts/          record_fixture.py — re-record the real Razorpay responses
  seed/             catalog.json, sessions.json
  keys/             ES256 keypairs (gitignored)
  docker-compose.yml
  .env.example
```
