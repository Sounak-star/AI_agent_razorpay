"""
Settings — loaded once at import time.

HARD BOOT GUARD: if RAZORPAY_KEY_ID does not start with 'rzp_test_',
the process exits immediately with a non-zero status code.
This is the last line of defence against accidentally wiring live keys.
"""

from __future__ import annotations

import sys
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Razorpay ───────────────────────────────────────────────────────────────
    RAZORPAY_KEY_ID: str = "rzp_test_placeholder"
    RAZORPAY_KEY_SECRET: str = "placeholder_secret"
    RAZORPAY_WEBHOOK_SECRET: str = ""

    # ── LLM provider ───────────────────────────────────────────────────────────
    # Groq and xAI are both OpenAI-compatible; see server/agents/llm.py.
    # Leave LLM_PROVIDER blank to infer from whichever key is set.
    GROQ_API_KEY: str = ""
    XAI_API_KEY: str = ""
    LLM_PROVIDER: str = ""            # "groq" | "xai" | "" (auto)
    # Hard deadline on any model call. Observed Groq latency reached 26.7s on
    # an upsell suggestion; a slow model must never hold a payment open.
    # 8s was tuned when the agent saw 20 short SKUs. The catalogue is 120 now
    # and a cart proposal carries far more context, measured at 3.1-4.3s and
    # spiking past 8s — which surfaced as APITimeoutError mid-demo.
    LLM_TIMEOUT_SECONDS: float = 20.0
    LLM_MODEL: str = ""               # blank = the provider's default

    # Published rate for LLM_MODEL, in USD per million tokens. Left at 0 the
    # calls are recorded as unpriced and the dashboard shows a dash, rather
    # than a cost figure derived from a rate nobody checked.
    LLM_PRICE_INPUT_USD_PER_MTOK: float = 0.0
    LLM_PRICE_OUTPUT_USD_PER_MTOK: float = 0.0


    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: str = "sqlite:///./tollgate.db"

    # ── Keypairs ───────────────────────────────────────────────────────────────
    BUYER_AGENT_PRIVATE_KEY_PATH: str = "./keys/buyer_es256.pem"
    BUYER_AGENT_PUBLIC_KEY_PATH: str = "./keys/buyer_es256_pub.pem"
    MERCHANT_AGENT_PRIVATE_KEY_PATH: str = "./keys/merchant_es256.pem"
    MERCHANT_AGENT_PUBLIC_KEY_PATH: str = "./keys/merchant_es256_pub.pem"

    # ── Feature flags ──────────────────────────────────────────────────────────
    # Enable POST /ledger/tamper (dev/demo only)
    ALLOW_TAMPER: bool = False
    # Replace LLM calls with recorded fixtures (CI / eval harness)
    STUB_MODE: bool = False

    # ── Payments ───────────────────────────────────────────────────────────────
    # How an approved escalation settles:
    #   synthetic  identifiers generated locally; no network at all
    #   replay     the recorded capture in evals/fixtures/razorpay_capture.json
    #   live       a real Razorpay test-mode order and payment link, then polled
    #              until a human pays it
    #
    # Defaults to synthetic on purpose. A stage demo that cannot run without
    # Razorpay being reachable is a demo that fails on stage; live is something
    # you switch on when you mean it, not something you inherit by accident.
    PAYMENTS_MODE: str = "synthetic"
    # How long the poller waits for a human to pay before giving up.
    PAYMENT_CAPTURE_TIMEOUT_SECONDS: float = 300.0
    PAYMENT_POLL_INTERVAL_SECONDS: float = 2.0
    # Razorpay settles captured payments on a T+N cycle. Used only to derive an
    # expected settlement date for display; the provider exposes no per-payment
    # settlement schedule endpoint, so this is a stated assumption, not a fact
    # read back from the API.
    SETTLEMENT_CYCLE_DAYS: int = 2
    # How long to wait before retrying a refund that was rejected for want of
    # settled balance. Checked against the expected settlement date, so a retry
    # is not attempted before it could possibly succeed.
    REFUND_RETRY_INTERVAL_SECONDS: int = 3_600

    # ── Policy limits ──────────────────────────────────────────────────────────
    DAILY_SPEND_CAP_PAISE: int = 5_000_000   # ₹50,000
    VELOCITY_MAX_TXN: int = 5
    VELOCITY_WINDOW_SECONDS: int = 3_600

    # ── Reconciler ─────────────────────────────────────────────────────────────
    RECONCILER_INTERVAL_SECONDS: int = 30
    ORPHANED_PAYMENT_TIMEOUT_SECONDS: int = 60
    # An active session with no ledger activity for this long is marked "stale"
    # so the dashboard stops presenting a hung session as live.
    STALE_SESSION_TIMEOUT_SECONDS: int = 60

    # ── MCP ────────────────────────────────────────────────────────────────────
    MERCHANT_ID: str = "merchant_tollgate_demo"
    MCP_MOUNT_PATH: str = "/mcp"

    @model_validator(mode="after")
    def _guard_test_key(self) -> "Settings":
        """Hard exit if a live Razorpay key is detected. Acceptance criterion."""
        if not self.RAZORPAY_KEY_ID.startswith("rzp_test_"):
            msg = (
                "\n"
                "╔══════════════════════════════════════════════════════════╗\n"
                "║  TOLLGATE — FATAL BOOT ERROR                             ║\n"
                "║                                                          ║\n"
                "║  RAZORPAY_KEY_ID does not start with 'rzp_test_'.        ║\n"
                "║  This system must NEVER run against live Razorpay keys.  ║\n"
                "║  Set a valid test key and restart.                       ║\n"
                "╚══════════════════════════════════════════════════════════╝\n"
            )
            print(msg, file=sys.stderr)
            sys.exit(1)
        return self


settings = Settings()
