"""
Policy codes — pure enums, no I/O, no dependencies.

ReasonCode is the machine-readable result carried by every Verdict.
The dashboard renders the code; it never renders a free-text LLM explanation.
"""

from enum import Enum


class Decision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


class ReasonCode(str, Enum):
    # Success
    ALLOW = "ALLOW"

    # DENY triggers
    PER_TXN_CAP = "PER_TXN_CAP"       # cart.total > intent.budget
    DAILY_CAP = "DAILY_CAP"             # rolling daily spend > limit
    CATEGORY_DENY = "CATEGORY_DENY"     # SKU category outside intent allowlist
    # Counts cart LINES, not units. See rule_item_count.
    ITEM_COUNT = "ITEM_COUNT"           # len(cart.items) > max_line_items
    MANDATE_INVALID = "MANDATE_INVALID" # verifier returned valid=False

    # ESCALATE triggers (human-in-the-loop required)
    VELOCITY = "VELOCITY"               # too many txns in rolling window
    PRICE_DRIFT = "PRICE_DRIFT"         # cart.total > estimate * 1.05
    # Named for what it actually tests: this BUYER has never transacted with
    # this merchant. It says nothing about the merchant being new to the
    # platform, which "NEW_MERCHANT" implied and the rule never checked.
    FIRST_CONTACT_BUYER = "FIRST_CONTACT_BUYER"
