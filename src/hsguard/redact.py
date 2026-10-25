"""Allow-list projection of Hyperswitch objects before they reach an LLM.

An allow-list (rather than a deny-list) means a new PII field added to the API
later stays hidden by default.
"""

from __future__ import annotations

import hashlib

PAYMENT_FIELDS = (
    "payment_id", "status", "amount", "amount_received", "amount_capturable", "currency",
    "created", "connector", "payment_method", "payment_method_type", "description",
    "error_code", "error_message", "capture_method",
)
REFUND_FIELDS = ("refund_id", "payment_id", "amount", "currency", "status", "reason",
                 "connector", "error_message", "created_at")


def _ref(value: str) -> str:
    return "ref_" + hashlib.sha256(value.encode()).hexdigest()[:12]


def project_refund(r: dict) -> dict:
    return {k: r[k] for k in REFUND_FIELDS if k in r and r[k] is not None}


def project_payment(p: dict) -> dict:
    out = {k: p[k] for k in PAYMENT_FIELDS if k in p and p[k] is not None}
    if p.get("customer_id"):
        # stable pseudonym so an agent can group payments by customer without seeing the id
        out["customer_ref"] = _ref(p["customer_id"])
    if p.get("refunds"):
        out["refunds"] = [project_refund(r) for r in p["refunds"]]
    hidden = sorted(k for k, v in p.items()
                    if k not in PAYMENT_FIELDS and k not in ("refunds",) and v not in (None, [], {}))
    if hidden:
        out["redacted_fields"] = hidden
    return out
