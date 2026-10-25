"""The guard: every agent action goes through here (policy, idempotency, redaction, audit)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .audit import AuditLog
from .client import HyperswitchClient, HyperswitchError
from .policy import Policy, evaluate_refund
from .redact import project_payment, project_refund


def refund_id_for(payment_id: str, amount: int, key: str) -> str:
    """Deterministic refund_id, so an agent that retries the same request cannot refund twice."""
    h = hashlib.sha256(f"{payment_id}|{amount}|{key}".encode()).hexdigest()[:24]
    return f"agt_{h}"


class Guard:
    def __init__(
        self,
        client: HyperswitchClient,
        policy: Policy,
        state_dir: str | Path,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.client = client
        self.policy = policy
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(self.state_dir / "audit.jsonl")
        self.now = now

    # ---- local state: pending approvals + refunds this guard executed ----
    @property
    def _state_path(self) -> Path:
        return self.state_dir / "state.json"

    def _load(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text())
        return {"pending": {}, "executed": []}

    def _save(self, st: dict) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(self._state_path)

    def spent_today(self, currency: str) -> int:
        day = self.now().date().isoformat()
        return sum(e["amount"] for e in self._load()["executed"]
                   if e["currency"] == currency and e["ts"][:10] == day)

    def _log(self, tool: str, args: dict, outcome: str, **extra) -> None:
        self.audit.append({"tool": tool, "args": args, "outcome": outcome, **extra}, now=self.now())

    # ---- read tools ----
    def get_payment(self, payment_id: str) -> dict:
        try:
            out = project_payment(self.client.get_payment(payment_id))
        except HyperswitchError as e:
            self._log("get_payment", {"payment_id": payment_id}, "error", status=e.status)
            return {"error": e.body, "status": e.status}
        self._log("get_payment", {"payment_id": payment_id}, "ok")
        return out

    def list_payments(self, limit: int = 10) -> list[dict]:
        limit = max(1, min(limit, 50))
        out = [project_payment(p) for p in self.client.list_payments(limit)]
        self._log("list_payments", {"limit": limit}, "ok", count=len(out))
        return out

    def list_refunds(self, payment_id: str) -> list[dict]:
        out = [project_refund(r) for r in self.client.list_refunds(payment_id)]
        self._log("list_refunds", {"payment_id": payment_id}, "ok", count=len(out))
        return out

    # ---- refund path ----
    def _context(self, payment_id: str) -> tuple[dict, list[dict]]:
        return self.client.get_payment(payment_id), self.client.list_refunds(payment_id)

    def preview_refund(self, payment_id: str, amount: int | None, currency: str | None,
                       reason: str | None) -> dict:
        args = {"payment_id": payment_id, "amount": amount, "currency": currency, "reason": reason}
        try:
            payment, refunds = self._context(payment_id)
        except HyperswitchError as e:
            self._log("preview_refund", args, "error", status=e.status)
            return {"error": e.body, "status": e.status}
        d = evaluate_refund(self.policy, payment, refunds, amount, currency, reason,
                            self.spent_today(payment.get("currency", "")))
        self._log("preview_refund", args, d.verdict, reasons=d.reasons)
        return {"dry_run": True, **d.to_dict()}

    def create_refund(self, payment_id: str, amount: int | None, currency: str | None,
                      reason: str | None, idempotency_key: str | None = None) -> dict:
        args = {"payment_id": payment_id, "amount": amount, "currency": currency,
                "reason": reason, "idempotency_key": idempotency_key}
        tool = "create_refund"
        try:
            payment, refunds = self._context(payment_id)
        except HyperswitchError as e:
            self._log(tool, args, "error", status=e.status)
            return {"status": "error", "error": e.body}

        if amount is not None:
            rid = refund_id_for(payment_id, amount, idempotency_key or reason or "")
            existing = next((r for r in refunds if r.get("refund_id") == rid), None)
            if existing:
                self._log(tool, args, "idempotent_replay", refund_id=rid)
                return {"status": "already_refunded", "note": "same request was executed before; nothing new sent",
                        "refund": project_refund(existing)}
            st = self._load()
            if rid in st["pending"]:
                self._log(tool, args, "already_pending", approval_id=rid)
                return {"status": "pending_human_approval", "approval_id": rid,
                        "note": "this exact request is already waiting for a human"}

        d = evaluate_refund(self.policy, payment, refunds, amount, currency, reason,
                            self.spent_today(payment.get("currency", "")))
        if d.verdict == "deny":
            self._log(tool, args, "denied", reasons=d.reasons)
            return {"status": "denied", **d.to_dict()}
        if d.verdict == "needs_approval":
            st = self._load()
            st["pending"][rid] = {"payment_id": payment_id, "amount": amount, "currency": currency,
                                 "reason": reason, "refund_id": rid, "requested_at": self.now().isoformat()}
            self._save(st)
            self._log(tool, args, "queued_for_approval", approval_id=rid, reasons=d.reasons)
            return {"status": "pending_human_approval", "approval_id": rid, **d.to_dict(),
                    "note": "a human must run `hsguard approve <approval_id>`; agents cannot approve"}
        return self._execute(tool, args, payment_id, amount, payment["currency"], reason, rid)

    def _execute(self, tool, args, payment_id, amount, currency, reason, rid, approver=None) -> dict:
        try:
            ref = self.client.create_refund(payment_id, amount, reason, rid)
        except HyperswitchError as e:
            self._log(tool, args, "error", status=e.status, error=e.body)
            return {"status": "error", "error": e.body}
        st = self._load()
        st["executed"].append({"ts": self.now().isoformat(), "refund_id": ref.get("refund_id", rid),
                               "payment_id": payment_id, "amount": amount, "currency": currency,
                               "approver": approver})
        st["pending"].pop(rid, None)
        self._save(st)
        self._log(tool, args, "refunded", refund_id=ref.get("refund_id"), approver=approver)
        return {"status": "refunded", "refund": project_refund(ref)}

    # ---- human-only operations (CLI, never exposed as MCP tools) ----
    def pending(self) -> dict:
        return self._load()["pending"]

    def approve(self, approval_id: str, approver: str) -> dict:
        req = self._load()["pending"].get(approval_id)
        if not req:
            return {"status": "error", "error": f"no pending approval {approval_id}"}
        payment, refunds = self._context(req["payment_id"])
        # re-check: the payment may have been refunded elsewhere since the request was queued
        d = evaluate_refund(self.policy, payment, refunds, req["amount"], req["currency"], req["reason"],
                            self.spent_today(payment["currency"]), approval_granted=True)
        args = {"approval_id": approval_id, "approver": approver}
        if d.verdict != "allow":
            self._log("approve", args, "denied", reasons=d.reasons)
            return {"status": "denied", **d.to_dict()}
        return self._execute("approve", args, req["payment_id"], req["amount"], payment["currency"],
                             req["reason"], approval_id, approver=approver)

    def reject(self, approval_id: str, approver: str) -> dict:
        st = self._load()
        if st["pending"].pop(approval_id, None) is None:
            return {"status": "error", "error": f"no pending approval {approval_id}"}
        self._save(st)
        self._log("reject", {"approval_id": approval_id, "approver": approver}, "rejected")
        return {"status": "rejected", "approval_id": approval_id}
