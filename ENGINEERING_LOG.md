# Tollgate — Engineering Log

## Day 1 (2026-08-29)

### What was built

Full end-to-end governed agentic-commerce rail in a single session. All eleven phases completed:

**Phase 1 — Scaffold & Config**
- FastAPI app with `lifespan` boot sequence.
- Boot-time guard: if `RAZORPAY_KEY_ID` doesn't start with `rzp_test_`, the process exits immediately with a printed banner. This is a hard acceptance criterion.
- SQLite with WAL mode for the default path. Postgres via docker-compose for concurrency tests. One-line switch: `DATABASE_URL` environment variable.

**Phase 2 — Mandate Layer (ES256 JWT)**
- `IntentMandate` and `CartMandate` schemas with canonical JSON hashing.
- ES256 keypair generation via `cryptography` library (no OpenSSL shell exec).
- Replay prevention: `MandateJti.jti` has a DB-level `UNIQUE` constraint. The authoritative check is `INSERT`-then-catch-`IntegrityError`, not check-then-insert.
- 11 tests, all green.

**Phase 3 — Policy Engine**
- 8 pure deterministic rules: `per_txn_cap`, `daily_cap`, `velocity`, `category_deny`, `price_drift`, `item_count`, `new_merchant`, `mandate_invalid`.
- Zero I/O, zero LLM calls, zero network in any rule.
- 27 tests, all green.

**Phase 4 — Ledger**
- Hash-chained append-only ledger. Each entry: `sha256(prev_hash + canonical_json(payload))`.
- `verify_chain()` re-derives every hash from scratch — any in-place mutation is detected.
- `replayed_from_fixture` boolean field on every entry: clearly distinguishes live events from harness replays.
- 13 tests, all green.

**Phase 5 — Payment Saga**
- **Option A (demo):** Real Razorpay Payment Link. Buyer pays; `--resume-payment <id>` records the fixture.
- **Option B (harness):** Fixture replay with synthetic IDs when no real fixture exists. `PAYMENT_SIMULATED` / `REFUND_SIMULATED` events with `replayed_from_fixture=True`. Zero real API calls.
- `StubNotImplemented` sentinel: STUB_MODE raises cleanly instead of silently doing nothing.

**Phase 6 — MCP Catalog**
- `sanitize()` strips all `_`-prefixed keys before any MCP response. Invariant assertion in `search_skus()` catches any regression.
- Product descriptions wrapped in `<<<PRODUCT_DESCRIPTION_START>>>`/`<<<PRODUCT_DESCRIPTION_END>>>` delimiters. System prompt flags these as untrusted user data.
- `get_authoritative_price()` is the single source of truth for prices. The LLM output is advisory only.
- 20 tests, all green — including explicit checks that `_has_injection` never appears in MCP output.

**Phase 7 — Buyer Agent**
- LLM proposes a list of SKU IDs only. It cannot set prices.
- `sign_cart_from_quote()` re-fetches catalog prices as a second verification layer.
- `--stub` flag swaps in fixture responses for CI runs.

**Phase 8 — REST API**
- Full session/checkout/ledger/escalation/metric/webhook surface.
- `POST /ledger/tamper` (gated behind `ALLOW_TAMPER=true`) for the live tamper → verify → "invalid" demo.
- `GET /.well-known/agent-commerce.json` for agent-commerce discovery.

**Phase 9 — Dashboard**
- Vite + React + TS + Tailwind, built to `dashboard/dist` and mounted by FastAPI at `/`. Mounted last so every API route wins the path match; one process serves API, MCP and UI.
- Four panels on a fixed 1280×720 frame, 2s poll: session stream, ledger, escalations, metrics strip. Plain `fetch` + `useState`/`useEffect` throughout — no query library, no component library, no charting library. The stacked bar is divs.
- **Client computes nothing.** `server/api/analytics.py` derives the verdict split, per-session elapsed time, latency, cost and the unauthorised-movement audit; the client formats and renders. This forced four backend additions: `GET /sessions`, pagination on `GET /sessions/{id}/ledger`, snapshots + a server-computed diff on `GET /escalations`, and an extended `GET /metrics`.
- **Cost and latency needed a real source.** Neither existed. Rather than estimate in the client, `EventType.LLM_CALL` was added and both live model call sites now record model, token usage, priced cost and wall-clock latency into the chain. Stub and harness runs make no model calls, so the API returns `null` with `samples: 0` and the tile renders a dash — an honest "no measurement" instead of a fabricated `$0.0000`.
- **Money moved without authorisation** is the headline tile. It is computed by walking the chain — a money-movement event is unauthorised unless an `ALLOW` verdict or `HUMAN_APPROVED` for that same session appeared at a lower seq — rather than by trusting the session status column. A forward single pass is what enforces "authorisation must precede the charge"; `test_analytics.py` pins that, plus the case where an `ALLOW` on one session must not cover a charge on another.
- Chain badge polls `/ledger/verify`. On a break the row at `broken_at_seq` fills red and every row after it dims to 30% — the break point reads from across a room without parsing a hash. Entries after the break are dimmed, never hidden: they still exist, they just can't be trusted.
- `REPLAYED` tag on any row with `replayed_from_fixture` or a `*_SIMULATED` event type. Showing the simulated legs is the honesty signal — the alternative is a demo that presents fixture replay as live settlement, which is the exact failure this system argues against.

**Phase 10 — Eval Harness**
- 5 normal buyer scenarios, 10 adversarial attacks.
- All 15 pass in 0.2s in stub mode.
- History injection from `_prior_spend_paise` / `_prior_txn_count` / `_expect_new_merchant` scenario fields — each attack tests exactly one rule.

**Phase 11 — Docs & Upsell**
- Upsell agent with headroom guard. LLM picks SKU ID; server verifies catalog price.
- README with 60-second quickstart.

### Bug fixes after first dashboard review

Three defects the dashboard made visible. All were real, and two were invisible from the API surface alone.

**1. Sessions recorded only 2 of 9 lifecycle events.** Not a retrieval bug — a write bug. `CATALOG_QUERIED`, `CART_BUILT` and `SESSION_CLOSED` had no `append()` call anywhere in the codebase, and the harness saga wrote only `POLICY_EVALUATED` and `PAYMENT_SIMULATED`. So the ledger could not show that anything had been checked before money moved, which is the one thing it exists to show.
Fixed by putting each event where its work happens rather than bolting them onto the saga: a shared `build_authoritative_cart()` (used by both the REST checkout and the harness) records the catalog lookup, the quote and the cart; `record_intent_signed()` records the mandate at the point of signing; `close_session()` is the single terminal transition and is idempotent. The harness now genuinely signs a cart mandate instead of logging that it did — `CART_SIGNED` has to correspond to a real signature. This also removed a duplicate `INTENT_SIGNED` on every live checkout. Completed sessions now carry 9 entries (10 with a refund), and `tests/test_lifecycle.py` asserts both the ≥8 count and the causal ordering.

**2. Stalled sessions sat in `active` forever.** The reconciler was running; its only sweep required `razorpay_order_id IS NOT NULL`, so any session that hung *before* the payment stage — the common case — was never a candidate. A second latent bug sat next to it: `cutoff` was timezone-aware while `updated_at` is naive, which under SQLite degrades into comparing strings with an offset suffix.
Added a stall sweep keyed on last ledger activity, with `SESSION_STALE` and a `stale` status surfaced in the UI. Sessions awaiting a human are exempt — a queue is not a hang. `stale` stops the elapsed clock but is excluded from latency samples, since folding a hang into a mean completion time would misreport it.

**3. Counters contradicted each other.** "6 ENTRIES" beside "1-2 of 2" beside "6 sessions / 3 verdicts" — three different populations, none labelled. The verdict split also counted ledger entries, so it could legitimately exceed the session count.
The split now counts each session once by its most recent verdict, guaranteeing `ALLOW + DENY + ESCALATE == total ≤ sessions_total`, and every count on screen names its population: chain badge `global · all sessions`, pager `… in session`, verdicts `N of M sessions reached a verdict`.

### Phase 9 follow-up — evidence, not decoration

- **Expandable ledger rows with client-side re-derivation.** `dashboard/src/canonical.ts` reimplements the server's canonical JSON so the browser can rebuild the exact preimage and hash it with WebCrypto. A viewer sees the payload, the preimage, both hashes, and `RE-DERIVED ✓` / `MISMATCH ✗` derived in their own browser — not the server's verdict repeated. Verified both ways: clean entries re-derive, and a tampered payload reports `MISMATCH` with visibly different hashes.
  - One bug found and fixed during that test: the effect's dependency array listed hash inputs by hand and omitted `payload`, so a tampered row kept its stale ✓ beside its new contents — the panel vouching for an entry it had never checked. The preimage string is now the dependency, so it cannot fall out of step with what is rendered.
  - Known limit, stated in the file: JSON cannot distinguish Python's `1.0` from `1`, so an integral float in a payload would produce a false mismatch. Every amount is integer paise and no payload contains a float.
- **Session trace strip.** INTENT → CATALOG → QUOTE → CART → ◆POLICY → ◆MANDATE → ORDER → PAYMENT → CLOSED, with the gates as diamonds. Step state comes from the verdict on the entry, not the session status: a DENY paints the POLICY gate red, an ESCALATE paints it amber (held, not failed), and `SESSION_CLOSED` stays green because closing cleanly is what it did. Marking CLOSED red instead would show the outcome while hiding which gate produced it.
- **Trigger-led escalation cards.** `differs` and `triggered` are separate server-side flags, because they are separate facts: a cart can differ in a field no rule examined, and VELOCITY fires on a rate with no cart field to point at (that card says so explicitly). Values that would truncate to identical strings are re-anchored to their differing segment.
- **Seed diversity.** `demo/seed.py` runs all ten `seed/sessions.json` scenarios through the real pipeline so all four terminal states are on screen at once. The scenarios' `_expected_outcome` is a label, never an instruction — the engine decides, and the seeder reports any disagreement rather than smoothing it over.

### Operator layer — answering "why this and not something else"

- **Cart contents, considered set, offers and denial counterfactuals** are all templated from ledger payloads in `server/api/narrative.py`. Nothing is model-generated: a narrative written by an LLM would be a claim *about* the trail rather than a reading *of* it.
- **The considered set is derived, not logged twice.** `CATALOG_QUERIED.sku_ids_returned` compared against `CART_BUILT.items` gives chosen and not-chosen without extending the payload. Where the server resolved SKUs by id there was no browsing step, and the line says so rather than implying a search that never happened.
- **The upsell agent was dead code.** `suggest_upsell` was never called from anywhere and no `UPSELL_*` event had ever been written to any ledger. It now runs after ALLOW and before the cart is signed, recording all four outcomes including *withheld* — an offer the headroom guard suppressed is a decision the rail made, and silence would hide it.
- **One signature, after the offer.** The first cut signed inside `run_upsell` *and* again in the saga, producing two `CART_SIGNED` entries with the first covering a cart that had already been superseded. The saga now signs once, after the offer, so the signature always covers the total actually paid.
- **Attach rate excludes sessions never offered anything.** A rate diluted by sessions that saw no offer measures reach rather than persuasiveness, and the two move independently. With no offers at all it returns `null` and the tile shows a dash.
- Seeding the offer path changed cart totals enough to trip DAILY_CAP a session early — real behaviour, but it made that scenario's own label wrong, so offers are disabled on the daily-cap chain to keep the threshold arithmetic exact.

### Collapsing the two sagas

The most serious defect found, and it was structural rather than a coding error.

`run_saga_demo` and `run_saga_harness` were separate implementations of the same
lifecycle. They drifted, and the drift landed on the control the whole system
rests on: **the harness path never called `verify_cart_mandate`.** A full
ten-attack run recorded **zero rows in `mandate_jtis`** — every eval result was
produced by a path that skipped mandate enforcement entirely.

The unit tests in `test_mandate.py` were never affected: they call the verifier
directly, so replay, expiry and hash-mismatch were always genuinely proven. What
was missing was evidence that the *saga* calls it — and on one path it did not.
A control that is unit-tested but unreachable from the path that runs in
production is not a control.

Collapsed into a single `run_saga()`. Live and seeded runs now differ only by
injected parameters: `payments=LIVE|REPLAY`, `cart_token`, `offer_upsell`. The
wrappers contain no behaviour, and a test asserts they contain no `evaluate(`,
`verify_cart_mandate`, `append(` or `close_session(` of their own — behaviour
cannot re-enter one path without the other. After the collapse the same run
records **84 rows, 52 of them cart mandates verified**.

Because `cart_token` became an injected parameter, five attacks that were
previously *inexpressible* could be written: replayed, expired, hash-mismatched,
unparseable, and unknown-intent mandates. Each fails for its own distinct
recorded reason.

**One of them failed on the first run, and it was the test, not the system.**
`11_mandate_replayed` came back ALLOW. The forge primed the replay before the
intent JTI existed, so the priming verification failed at `intent_jti_not_found`
and rolled back the cart-JTI insert — nothing was burned and the attack tested
nothing. Replay protection was confirmed working by direct test. The forge now
records the intent JTI under the session's own id first, and **raises if priming
fails** rather than silently passing a scenario that proves nothing.

### Tightening the eval contract

`04_item_count_overflow` passed for its entire existence without ever reaching
the rule it was named for. It listed `GRO011`/`GRO012`, which do not exist, so
the cart never built and the resulting `CartBuildError` was caught as `error` —
and the pass condition accepted `error` as satisfying `deny`.

That was one instance of a general looseness: expectations were a single word
(`deny`), so an attack could also pass because a *different, earlier* rule
stopped it. Both are the same failure — a defence reported as holding when it
was never exercised.

Attacks now declare `expect: {decision, code}` and are judged against the
`POLICY_EVALUATED` entry the ledger recorded. `ERROR` is a third state alongside
`PASS`/`FAIL`, counted and reported separately, and can never satisfy an
expectation. The five mandate attacks additionally declare `reason_contains`,
because they all land on `MANDATE_INVALID` and only the verifier's reason
distinguishes them.

Verified by deliberately breaking each case: reintroducing the bad SKU now
reports `ERROR` with the exception, and pointing an attack at the wrong reason
code now reports `FAIL`.

### Naming a rule for what it counts

`rule_item_count` compares `len(cart.items)` — lines, not units. The mandate
field was called `max_items`, which said neither. A session authorising "max 4
items" allowed a cart of 2 lines totalling 10 units, correctly, and nothing on
screen made that reading available.

Renamed to `max_line_items`, with `max_items` kept as a pydantic alias: it is a
claim inside signed intent mandates, and a hard rename would invalidate every
mandate already issued. Verdict details and the operator narrative now state
lines and units separately, so a reader never has to infer which was checked.

No unit ceiling was added. Spend is already bounded by `per_txn_cap` and
`daily_cap`, so quantity is constrained by value; adding a count-based DENY at
freeze time would have changed verdicts on existing data without being asked
for. Recorded as a policy question rather than silently answered.

### Showing the comparison the rule made

The `PRICE_DRIFT` card contradicted itself: cause text said 899% above estimate
while the diff showed total ₹999 against an authorised ₹5,000. Both were
accurate — the diff was showing the `PER_TXN_CAP` comparison on a card about
drift.

Cart-derived rules now carry a `comparison` naming the two sides they weighed
and the threshold between them. It leads the card; everything else collapses
behind a toggle. A card that lists every field of the mandate invites the reader
to compare the wrong pair.

### Reading the report, and reading the screen

Two presentation faults with the same shape: correct data arranged so it says
something untrue.

The eval report marked every scenario with ✅ or ❌, so three correct denials
read as three failures in a run where the engine had done exactly its job. It
now reports the **verdict** — ALLOW / DENY / ESCALATE with its reason code — and
whether it **matched the scenario's expectation** as a separate column. Only a
mismatch or an error carries a warning mark.

The forensic view showed three different sessions at once: the ledger filtered
to one, the escalations rail listing others. The rail now follows the selection
and says "No escalations for this session" with the count pending elsewhere and
a way back. `prev_hash` left the table — two truncated hex columns side by side
invite the eye to match strings it cannot verify by looking, and the linkage is
checked in the expanded row where the hash is re-derived. The hash input moved
behind a toggle: it is the tallest block on screen and the least read, and it
belongs in a drill-down rather than the default state.

Row expansion was already collapsed-by-default and one-at-a-time; the height was
the actual complaint, and hiding the preimage is what fixed it.

### Test summary

```
180/180 unit tests  (6.9s)      [as of Day 1; see Day 2 for current figures]
 15/15 adversarial   (exact verdict + reason match)
 23    normal eval   (stub mode)
 15/15 adversarial   (stub mode)
```

### Design decisions and tradeoffs

**Fixture replay vs. fabricated IDs**
The earlier approach of calling the real Razorpay refund API with a fabricated payment_id was rejected. A deliberate 400 in the ledger looks like a bug under review. The correct approach: one real Option A run records the fixture; harness replays the real IDs. When no fixture exists (CI, first run), synthetic IDs are used and `synthetic: true` is set in the ledger payload.

**Injection defence framing**
Regex stripping of injection patterns is not the defence — it's trivially bypassed. The real defence is architectural:
1. LLM cannot compute totals (`get_authoritative_price()` is server-side).
2. LLM cannot set policy verdicts (`evaluate()` is pure, no LLM path).
3. LLM cannot mint a CartMandate (`sign_cart()` is server-only, uses catalog prices).
4. Descriptions are wrapped in delimiters (defence-in-depth, not primary defence).

**SQLite WAL vs. Postgres**
SQLite WAL mode handles the UNIQUE(jti) constraint correctly in serial execution. True concurrent write races (double_charge, refund_race) require Postgres. This is documented in the eval report and README, not papered over.

**MCP transport**
Single FastAPI process, MCP mounted at `/mcp` via `mcp.streamable_http_app()`. One log stream, shared DB session, no inter-process boundary for a 7-day build.

### Known gaps

- Mean cost per session reads as a dash until a live (non-stub) run records `LLM_CALL` entries. This is deliberate — see Phase 9 — but it does mean the tile is empty on a fresh stub demo.
- Merchant agent is the MCP tool surface itself; a separate Python class adds no value.
- `double_charge` and `refund_race` concurrency tests require Postgres; noted in report.

## Day 2 (2026-09-01 — 2026-09-02)

Factual list of what changed. Prose to be rewritten.

### Live Razorpay payment path

- `server/payments/confirm.py` (new). `CaptureResult`, `PaymentConfirmer`
  Protocol, `PollingConfirmer` (2s interval, 5-min timeout), `FixtureConfirmer`,
  `SyntheticConfirmer`. Polling rather than webhooks: no public tunnel needed.
  A webhook receiver implements the same protocol without the saga changing.
- `server/payments/fixtures.py` (new). Verbatim recording store, kinds
  `order | payment_link | payment | refund_attempt`.
- `server/payments/settlement.py` (new). Settlement status and expected date.
- `scripts/record_fixture.py` (new). The only thing that writes the fixture.
- `PaymentMode` split `LIVE | REPLAY | SYNTHETIC`. `--payments=` flag on
  `demo/run.py` and `evals/harness.py`. Default synthetic: a full demo runs with
  zero network calls, verified with the fixture deleted from disk.
- `open_live_payment()` / `await_live_capture()` split so the approving HTTP
  request returns the payment URL immediately and the poll runs on its own
  thread.
- `create_refund` reissued over plain HTTP rather than the SDK: the SDK raises
  `BadRequestError(description)` and discards the error envelope, so the code,
  metadata, reason, source and step were all being thrown away.
- Ledger events added: `REFUND_FAILED`, `REFUND_PENDING_SETTLEMENT`,
  `REFUND_RETRY_SCHEDULED`.

### The refund rejection, and its verified cause

- Refunding the captured test payment returns HTTP 400 `BAD_REQUEST_ERROR`,
  description "invalid request sent", empty metadata, reason/source/step "NA".
- Cause verified from Razorpay's dashboard: "Your account does not have
  sufficient balance to instantly refund this payment." Payment captured but
  unsettled; T+2. Corroborated independently: `GET /v1/settlements` returns
  `count: 0`.
- `REFUND_PENDING_SETTLEMENT` kept distinct from `REFUND_FAILED` — one is
  retryable after settlement, the other is not. The split is only made when
  settlement status is actually known; an unverified "it will work later"
  converts a permanent failure into a silent retry loop.
- Reconciler sweeps pending refunds, retries after the expected settlement date,
  backs off between attempts, and never sweeps such a session as stale.
- Settlement date derived from the provider's capture time, not our ledger write
  time — the two differ by the poll duration and put the date a day out.

### Approving an escalation did nothing

- `POST .../approve` recorded `HUMAN_APPROVED`, set the session active, and
  stopped. The reconciler then swept the idle session to `STALE`. Ledger read
  `HUMAN_APPROVED → SESSION_STALE` with no payment between them.
- Settlement extracted into `settle_authorised_cart()`, called by both
  `run_saga` on an ALLOW and the approve endpoint. One settlement path.
- `PAYMENTS_MODE=live` with `STUB_MODE=on` is refused rather than falling back
  to synthetic, which would have looked identical to a real payment.

### Provenance

- `fixture_path` was keyed off whether the fixture file *existed*, so synthetic
  runs cited a recording they had never opened. Now keyed off whether that event
  was read from it.
- The path recorded was absolute (`C:\Users\...`) and was being hashed into the
  ledger. Now repo-relative.
- `SESSION_CLOSED` inherited REPLAYED from the settlement before it. Provenance
  badges now enforced in `chain.append()` against `PROVENANCE_BEARING_EVENTS`
  (ORDER_*, PAYMENT_*, REFUND_*), so no call site can reintroduce it.
- Replayed capture events record `replayed_amount_paise` and `replayed_fields`:
  the ids are real, the amount is the scenario's. Without this a REPLAYED badge
  over a ₹479 cart read as "₹479 really moved" when the recording is for ₹1.
- The fixture was silently overwritten by any live API call, leaving an unpaid
  ₹799 order beside a ₹1 payment from a different order. Recording is now off
  unless explicitly enabled, and `coherence_problem()` refuses to replay a set
  whose parts were never one transaction.

### False greens

- `demo/seed.py` printed `refund 1 ok` while zero refund events existed and no
  session was in a refunded state. Outcome was read from the scenario's own
  `_simulate_refund` flag, never from the ledger.
- `evals/harness.py` mapped `{"refund": "ALLOW"}`, so a refund expectation was
  satisfied by a policy ALLOW regardless of whether a refund happened.
- The refund leg produced nothing on the replay path: `_settle_replay` returned
  `refund_id=None` unconditionally, so `simulate_refund=True` wrote no event.
- The harness printed `[OK] All checks passed` and exited 0 with three
  mismatched scenarios in the report; only exceptions counted toward the exit
  code.
- `server/ledger/outcomes.py` (new) derives a session's outcome from its ledger
  alone. Both the seeder and the report use it, so they cannot disagree. A
  rejected refund never satisfies an expectation of "refund".

### Latency attribution

- Reported engine mean went from 245ms to 34.4s after two escalations were
  approved by hand. Nothing was slower: a person taking six minutes to click a
  button was being counted as compute.
- Engine time is now wall clock minus model, provider and human waits, with
  `human_wait` and `provider_wait` reported as their own metrics and shown in
  the strip.

### Duplicate definitions

Twice a refactor left two definitions of the same function in `saga.py`, and the
later one silently won — once an `attempt_refund` returning a bool instead of
classifying the rejection, once a `_settle_replay` with no coherence guard.
Neither failed loudly. `tests/test_payments_live.py` now asserts no top-level
name is defined twice across eight modules.

### Stale references corrected

`.env.example` set `XAI_API_KEY` only, so a clean clone ran xAI rather than the
Groq model in use; `buyer.py` docstring said "Uses xAI grok-3-mini";
`llm_cost.py` priced `claude-3-5-haiku-20241022`, a model this build has never
called; `conftest.py` set `ANTHROPIC_API_KEY`. No rate is hardcoded for
qwen/qwen3.8-27b because none has been verified.

### Test summary

```
244 passed             unit tests
 15/15 adversarial     (exact verdict + reason match)
 23    normal eval     — 3 do not match the ledger, harness exits 1
```

### Open, not fixed

- No session has run end to end against the real API in this build: real order
  and link creation was verified, and confirm-plus-refund was verified against
  the already-paid link, but no freshly created link has been paid and carried
  through. Paying one requires a human at Razorpay's checkout.
- `REFUND_FAILED` has never fired on a live path; `REFUND_PENDING_SETTLEMENT`
  does, which is correct given the verified cause.
- `evals/harness.py` never passes `_simulate_refund`, so `sess_008` reports
  `refund_state=none` there and `failed` under the seeder.
- `sess_019_velocity_run_6` (expected ESCALATE, recorded ALLOW) and
  `sess_023_daily_cap_bulk_4` (expected DENY, recorded ALLOW) fail in the
  harness but fire correctly under `demo/seed.py`. The two build buyer history
  differently.
- REFUNDED is not a reachable terminal state in the seed set while the recorded
  refund is a rejection.
- The project is not a git repository, so the documented `git clone` step
  cannot be followed.
