"""Refund policy: the rules an AI agent's refund request must pass before it reaches Hyperswitch.

All amounts are minor units. Limits are per currency; a currency with no limit
configured is refused outright, so a new currency is opt-in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

REFUNDABLE_STATUSES = {"succeeded", "partially_captured"}
COUNTED_REFUND_STATUSES = {"succeeded", "pending", "review"}
HYPERSWITCH_MIN_REFUND = 100  # spec: RefundRequest.amount minimum


@dataclass
class Policy:
    # agent may refund up to this per call without a human
    auto_approve_limit: dict[str, int] = field(default_factory=lambda: {"INR": 500000, "USD": 5000})
    # nothing above this goes through, even with approval
    hard_limit: dict[str, int] = field(default_factory=lambda: {"INR": 2000000, "USD": 50000})
    # total agent-initiated refunds per UTC day
    daily_budget: dict[str, int] = field(default_factory=lambda: {"INR": 1000000, "USD": 20000})
    min_reason_chars: int = 8

    @classmethod
    def load(cls, path: str | Path | None) -> "Policy":
        if not path:
            return cls()
        return cls(**json.loads(Path(path).read_text()))


@dataclass
class Decision:
    verdict: str  # "allow" | "needs_approval" | "deny"
    reasons: list[str]
    remaining_refundable: int | None = None

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons,
                "remaining_refundable": self.remaining_refundable}


def remaining_refundable(payment: dict, refunds: list[dict]) -> int:
    received = payment.get("amount_received") or 0
    used = sum(r.get("amount", 0) for r in refunds if r.get("status") in COUNTED_REFUND_STATUSES)
    return max(received - used, 0)


def evaluate_refund(
    policy: Policy,
    payment: dict,
    refunds: list[dict],
    amount: int | None,
    currency: str | None,
    reason: str | None,
    spent_today: int,
    approval_granted: bool = False,
) -> Decision:
    deny: list[str] = []
    cur = payment.get("currency")
    remaining = remaining_refundable(payment, refunds)

    if amount is None:
        deny.append("amount is required: Hyperswitch refunds the FULL payment amount when it is omitted")
    elif not isinstance(amount, int) or amount < HYPERSWITCH_MIN_REFUND:
        deny.append(f"amount must be an integer in minor units, at least {HYPERSWITCH_MIN_REFUND}")
    if currency != cur:
        deny.append(f"currency mismatch: request says {currency!r}, payment is {cur!r}")
    if payment.get("status") not in REFUNDABLE_STATUSES:
        deny.append(f"payment status {payment.get('status')!r} is not refundable")
    if not reason or len(reason.strip()) < policy.min_reason_chars:
        deny.append(f"a reason of at least {policy.min_reason_chars} characters is required")
    if cur not in policy.hard_limit:
        deny.append(f"no refund limits configured for currency {cur!r}")

    if deny or amount is None:
        return Decision("deny", deny, remaining)

    if amount > remaining:
        deny.append(f"amount {amount} exceeds remaining refundable {remaining}")
    if amount > policy.hard_limit[cur]:
        deny.append(f"amount {amount} is above the hard limit {policy.hard_limit[cur]} {cur}")
    budget = policy.daily_budget.get(cur, 0)
    if spent_today + amount > budget:
        deny.append(f"daily agent refund budget exceeded: {spent_today} already used of {budget} {cur}")
    if deny:
        return Decision("deny", deny, remaining)

    if amount > policy.auto_approve_limit.get(cur, 0) and not approval_granted:
        return Decision("needs_approval",
                        [f"amount {amount} is above the auto-approve limit "
                         f"{policy.auto_approve_limit.get(cur, 0)} {cur}; queued for a human"], remaining)
    return Decision("allow", ["within policy"], remaining)
