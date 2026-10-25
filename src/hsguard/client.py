"""Thin Hyperswitch REST client (v1 API, `api-key` header auth).

Only the calls the guard needs. Amounts are always minor units (paisa, cents),
exactly as the Hyperswitch API takes them.
"""

from __future__ import annotations

from typing import Any

import httpx

SANDBOX_URL = "https://sandbox.hyperswitch.io"


class HyperswitchError(Exception):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"Hyperswitch returned HTTP {status}: {body}")


class HyperswitchClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = SANDBOX_URL,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
    ):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"api-key": api_key, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    def _do(self, method: str, path: str, **kw: Any) -> Any:
        resp = self._http.request(method, path, **kw)
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400:
            raise HyperswitchError(resp.status_code, body)
        return body

    def get_payment(self, payment_id: str) -> dict:
        return self._do("GET", f"/payments/{payment_id}")

    def list_payments(self, limit: int = 10) -> list[dict]:
        body = self._do("GET", "/payments/list", params={"limit": limit})
        # The spec documents PaymentListResponse {size, data}; tolerate a bare list too.
        return body.get("data", []) if isinstance(body, dict) else body

    def list_refunds(self, payment_id: str) -> list[dict]:
        body = self._do("POST", "/refunds/list", json={"payment_id": payment_id, "limit": 100})
        return body.get("data", []) if isinstance(body, dict) else body

    def create_refund(self, payment_id: str, amount: int, reason: str, refund_id: str) -> dict:
        return self._do(
            "POST",
            "/refunds",
            json={
                "payment_id": payment_id,
                "amount": amount,
                "reason": reason,
                "refund_id": refund_id,
                "refund_type": "instant",
            },
        )

    def get_refund(self, refund_id: str) -> dict:
        return self._do("GET", f"/refunds/{refund_id}")
