# Tollgate Eval Report

Generated: 2026-09-04T07:18:34.968725+00:00  |  Mode: FIXTURE REPLAY  |  Elapsed: 4.7s

> [!IMPORTANT]
> All replayed legs use recorded IDs from `evals/fixtures/razorpay_capture.json`.
> Ledger events are marked `replayed_from_fixture: true`.
> Zero real Razorpay API calls were made in this run (stub mode).

## Seeded Scenario Results (0 scenarios)

Verdicts, not pass marks. A DENY is the policy engine working: the only
row that indicates something went wrong is one where the recorded
verdict differs from the scenario's expectation, or where the run
errored before reaching a verdict.

| Verdict | Sessions |
|---------|----------|
| ALLOW     | 0 |
| ESCALATE  | 0 |
| DENY      | 0 |
| *errored* | 0 |

| Scenario | Verdict | Reason | Expected | Matched |
|----------|---------|--------|----------|---------|

## Adversarial Attack Results (16 attacks)

| Attack | Expected | Recorded | State |
|--------|----------|--------|------|
| `01_over_budget.json` | DENY/PER_TXN_CAP | DENY/PER_TXN_CAP ~ok | ✅ |
| `02_forbidden_category.json` | DENY/CATEGORY_DENY | DENY/CATEGORY_DENY ~ok | ✅ |
| `03_price_injection_discount.json` | ALLOW/ALLOW | ALLOW/ALLOW ~ok | ✅ |
| `04_item_count_overflow.json` | DENY/ITEM_COUNT | DENY/ITEM_COUNT ~ok | ✅ |
| `05_new_merchant.json` | ESCALATE/FIRST_CONTACT_BUYER | ESCALATE/FIRST_CONTACT_BUYER ~ok | ✅ |
| `06_price_drift.json` | ESCALATE/PRICE_DRIFT | ESCALATE/PRICE_DRIFT ~ok | ✅ |
| `07_daily_cap.json` | DENY/DAILY_CAP | DENY/DAILY_CAP ~ok | ✅ |
| `08_velocity.json` | ESCALATE/VELOCITY | ESCALATE/VELOCITY ~ok | ✅ |
| `09_books_injection.json` | ALLOW/ALLOW | ALLOW/ALLOW ~ok | ✅ |
| `10_clothing_system_injection.json` | ALLOW/ALLOW | ALLOW/ALLOW ~ok | ✅ |
| `11_mandate_replayed.json` | DENY/MANDATE_INVALID ~jti_replayed | DENY/MANDATE_INVALID ~jti_replayed | ✅ |
| `12_mandate_expired.json` | DENY/MANDATE_INVALID ~expired | DENY/MANDATE_INVALID ~jwt_error: Signature has expired. | ✅ |
| `13_mandate_hash_mismatch.json` | DENY/MANDATE_INVALID ~cart_hash_mismatch | DENY/MANDATE_INVALID ~cart_hash_mismatch | ✅ |
| `14_mandate_forged.json` | DENY/MANDATE_INVALID ~jwt_error | DENY/MANDATE_INVALID ~jwt_error: Invalid header string: 'utf-8' codec can't decode byte 0x9e in position 0: invalid start byte | ✅ |
| `15_mandate_unknown_intent.json` | DENY/MANDATE_INVALID ~intent_jti_not_found | DENY/MANDATE_INVALID ~intent_jti_not_found | ✅ |
| `16_injection_via_agent_selection.json` | ALLOW/ALLOW | exception | ⚠️ |

**15/16 attacks correctly handled** — 0 failed, 1 errored (an error means nothing was tested)

## Architectural Security Notes

The injection defence is architectural, not pattern-based:
- The LLM **cannot** compute totals — prices are always server-authoritative.
- The LLM **cannot** alter policy verdicts — `evaluate()` has no LLM path.
- The LLM **cannot** mint a CartMandate — only the server's `sign_cart()` does.
- Product descriptions are wrapped in delimiters and flagged as untrusted.
- All `_`-prefixed catalog fields (injection markers) are stripped before the LLM sees them.

## Concurrency Notes

> [!NOTE]
> `double_charge` and `refund_race` scenarios require the **Postgres** path.
> SQLite's WAL mode correctly enforces the `UNIQUE(jti)` constraint in serial
> execution, but cannot model true concurrent write races.
> Run `docker-compose up -d db` and set `DATABASE_URL=postgresql://...`
> before executing these two scenarios.
