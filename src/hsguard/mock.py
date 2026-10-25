"""In-memory stand-in for the Hyperswitch v1 API, wired in as an httpx transport.

Field names and the "omitted refund amount defaults to the full payment amount"
rule follow the public OpenAPI spec (api-reference/v1/openapi_spec_v1.json).
It is a fixture for tests and the demo, not a reimplementation of Hyperswitch.
"""

from __future__ import annotations

import copy
import json
import re

import httpx

MOCK_API_KEY = "snd_mock_key"


def seed_payments() -> dict[str, dict]:
    pii = {
        "client_secret": "pay_demo_001_secret_9f8a7b",
        "email": "asha.k@example.com",
        "name": "Asha K",
        "phone": "9876543210",
        "billing": {"address": {"line1": "12 MG Road", "city": "Bengaluru", "zip": "560001"}},
        "payment_method_data": {"card": {"last4": "4242", "card_holder_name": "Asha K"}},
        "customer": {"id": "cus_88", "email": "asha.k@example.com"},
        "customer_id": "cus_88",
    }
    base = {"merchant_id": "merchant_demo", "connector": "stripe_test", "payment_method": "card",
            "created": "2026-10-20T10:00:00Z", "refunds": None}
    return {
        "pay_demo_001": {**base, **pii, "payment_id": "pay_demo_001", "status": "succeeded",
                         "amount": 250000, "amount_received": 250000, "currency": "INR",
                         "description": "Order #A-1001"},
        "pay_demo_002": {**base, **pii, "payment_id": "pay_demo_002", "status": "succeeded",
                         "amount": 1200000, "amount_received": 1200000, "currency": "INR",
                         "description": "Order #A-1002"},
        "pay_demo_003": {**base, "payment_id": "pay_demo_003", "status": "requires_payment_method",
                         "amount": 99900, "amount_received": 0, "currency": "INR",
                         "description": "Abandoned checkout"},
        "pay_demo_004": {**base, **pii, "payment_id": "pay_demo_004", "status": "succeeded",
                         "amount": 4999, "amount_received": 4999, "currency": "USD",
                         "description": "Order #US-77"},
    }


class FakeHyperswitch:
    def __init__(self) -> None:
        self.payments = seed_payments()
        self.refunds: dict[str, dict] = {
            "ref_seed_1": {"refund_id": "ref_seed_1", "payment_id": "pay_demo_004", "amount": 2000,
                           "currency": "USD", "status": "succeeded", "connector": "stripe_test",
                           "reason": "damaged item"},
        }
        self.create_calls = 0

    @staticmethod
    def _err(status: int, code: str, msg: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"type": "invalid_request", "code": code, "message": msg}})

    def _refunded(self, payment_id: str) -> int:
        return sum(r["amount"] for r in self.refunds.values()
                   if r["payment_id"] == payment_id and r["status"] in ("succeeded", "pending"))

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("api-key") != MOCK_API_KEY:
            return self._err(401, "IR_01", "API key not provided or invalid API key used")
        path, method = request.url.path, request.method

        if method == "GET" and path == "/payments/list":
            limit = int(request.url.params.get("limit", 10))
            data = [copy.deepcopy(p) for p in self.payments.values()][:limit]
            return httpx.Response(200, json={"size": len(data), "data": data})

        if m := re.fullmatch(r"/payments/([\w-]+)", path):
            p = self.payments.get(m.group(1))
            if method != "GET" or p is None:
                return self._err(404, "HE_02", "Payment does not exist in our records")
            out = copy.deepcopy(p)
            out["refunds"] = [r for r in self.refunds.values() if r["payment_id"] == p["payment_id"]] or None
            return httpx.Response(200, json=out)

        if method == "POST" and path == "/refunds":
            body = json.loads(request.content or b"{}")
            p = self.payments.get(body.get("payment_id", ""))
            if p is None:
                return self._err(404, "HE_02", "Payment does not exist in our records")
            if body.get("refund_id") in self.refunds:
                return self._err(400, "HE_01", "Duplicate refund request. Refund already attempted with the refund ID")
            amount = body.get("amount")
            if amount is None:  # API default: refund the full payment amount
                amount = p["amount"]
            if amount > p["amount_received"] - self._refunded(p["payment_id"]):
                return self._err(400, "IR_13", "The refund amount exceeds the amount captured")
            self.create_calls += 1
            rid = body.get("refund_id") or f"ref_mock_{self.create_calls}"
            ref = {"refund_id": rid, "payment_id": p["payment_id"], "amount": amount,
                   "currency": p["currency"], "status": "succeeded", "connector": p["connector"],
                   "reason": body.get("reason")}
            self.refunds[rid] = ref
            return httpx.Response(200, json=ref)

        if method == "POST" and path == "/refunds/list":
            body = json.loads(request.content or b"{}")
            data = [r for r in self.refunds.values() if r["payment_id"] == body.get("payment_id")]
            return httpx.Response(200, json={"count": len(data), "total_count": len(data), "data": data})

        if m := re.fullmatch(r"/refunds/([\w-]+)", path):
            r = self.refunds.get(m.group(1))
            return httpx.Response(200, json=r) if r else self._err(404, "HE_02", "Refund does not exist")

        return self._err(404, "IR_00", f"Unrecognized request URL {method} {path}")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
