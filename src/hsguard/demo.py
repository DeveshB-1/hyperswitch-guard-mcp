"""Scripted agent session against the mock Hyperswitch, driven through a real MCP client.

Every agent step below is an MCP `tools/call`; the human approval step uses the
CLI path (Guard.approve), which has no MCP tool.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from mcp.client.client import Client

from .client import HyperswitchClient
from .guard import Guard
from .mock import MOCK_API_KEY, FakeHyperswitch
from .policy import Policy
from .server import build_server


def _result(res) -> dict:
    if getattr(res, "structured_content", None):
        return res.structured_content
    return json.loads(res.content[0].text)


def _fmt(minor: int, cur: str) -> str:
    sym = {"INR": "Rs ", "USD": "$"}.get(cur, cur + " ")
    return f"{sym}{minor / 100:,.2f}"


async def run_demo(state_dir: str | None = None) -> dict:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    fake = FakeHyperswitch()
    client = HyperswitchClient(MOCK_API_KEY, base_url="https://sandbox.hyperswitch.io",
                               transport=fake.transport())
    state = Path(state_dir or tempfile.mkdtemp(prefix="hsguard-demo-"))
    guard = Guard(client, Policy(), state)
    server = build_server(guard)
    steps: list[tuple[str, str]] = []

    async with Client(server) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        print(f"MCP tools exposed: {', '.join(tools)}\n")

        async def call(label: str, name: str, **args) -> dict:
            out = _result(await c.call_tool(name, args))
            status = out.get("status") or out.get("verdict") or "ok"
            why = "; ".join(out.get("reasons", [])) if status != "refunded" else ""
            steps.append((label, status))
            print(f"[agent] {label}\n        -> {status}" + (f": {why}" if why else ""))
            return out

        p = _result(await c.call_tool("get_payment", {"payment_id": "pay_demo_001"}))
        print("[agent] get_payment pay_demo_001")
        print(f"        -> {p['status']} {_fmt(p['amount'], p['currency'])}; "
              f"hidden from the model: {', '.join(p['redacted_fields'])}")

        await call("refund pay_demo_001, no amount given", "create_refund",
                   payment_id="pay_demo_001", currency="INR", reason="customer asked for a refund")
        await call("refund Rs 500 on pay_demo_001", "create_refund", payment_id="pay_demo_001",
                   amount=50000, currency="INR", reason="duplicate charge on order A-1001")
        before = fake.create_calls
        await call("same Rs 500 refund again (agent retry)", "create_refund", payment_id="pay_demo_001",
                   amount=50000, currency="INR", reason="duplicate charge on order A-1001")
        retry_sent = fake.create_calls - before
        await call("refund Rs 2,500 on pay_demo_001 (only Rs 2,000 left)", "create_refund",
                   payment_id="pay_demo_001", amount=250000, currency="INR",
                   reason="customer cancelled whole order")
        await call("refund 2000 'INR' on a USD payment", "create_refund", payment_id="pay_demo_004",
                   amount=2000, currency="INR", reason="partial return of order US-77")
        await call("refund an unpaid checkout", "create_refund", payment_id="pay_demo_003",
                   amount=99900, currency="INR", reason="customer says they were charged")
        queued = await call("refund Rs 8,000 on pay_demo_002", "create_refund", payment_id="pay_demo_002",
                            amount=800000, currency="INR", reason="order A-1002 returned in full")

        approved = guard.approve(queued["approval_id"], approver="ops@merchant")
        steps.append(("human approves the Rs 8,000 refund", approved["status"]))
        print(f"[human] hsguard approve {queued['approval_id']}\n        -> {approved['status']}")

        await call("another Rs 3,000 refund the same day", "create_refund", payment_id="pay_demo_002",
                   amount=300000, currency="INR", reason="goodwill credit for late delivery")

    ok, msg = guard.audit.verify()
    print(f"\nAudit log: {msg}")
    lines = guard.audit.path.read_text().splitlines()
    e = json.loads(lines[2])
    e["outcome"] = "ok"  # someone edits one entry after the fact
    lines[2] = json.dumps(e, sort_keys=True)
    tampered = state / "audit.tampered.jsonl"
    tampered.write_text("\n".join(lines) + "\n")
    from .audit import AuditLog
    t_ok, t_msg = AuditLog(tampered).verify()
    print(f"Tampered copy: {t_msg}")

    refunded = sum(1 for _, s in steps if s == "refunded")
    blocked = sum(1 for _, s in steps if s == "denied")
    summary = {"agent_refund_requests": sum(1 for lbl, _ in steps if not lbl.startswith("human")),
               "executed": refunded, "denied": blocked, "queued_for_human": 1,
               "duplicate_requests_sent_on_retry": retry_sent,
               "hyperswitch_refund_calls": fake.create_calls,
               "audit_ok": ok, "tamper_detected": not t_ok}
    print("\nSummary:", json.dumps(summary))
    return summary
