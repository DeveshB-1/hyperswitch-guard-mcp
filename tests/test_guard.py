import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from hsguard.audit import AuditLog
from hsguard.client import HyperswitchClient
from hsguard.demo import _result
from hsguard.guard import Guard
from hsguard.mock import MOCK_API_KEY, FakeHyperswitch
from hsguard.policy import Policy, evaluate_refund
from hsguard.redact import project_payment


@pytest.fixture
def env(tmp_path):
    fake = FakeHyperswitch()
    client = HyperswitchClient(MOCK_API_KEY, transport=fake.transport())
    clock = {"now": datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)}
    guard = Guard(client, Policy(), tmp_path / "state", now=lambda: clock["now"])
    return fake, guard, clock


# ---------- policy ----------

def pay(**kw):
    return {"payment_id": "p", "status": "succeeded", "amount": 10000, "amount_received": 10000,
            "currency": "INR", **kw}


def test_missing_amount_is_denied_because_api_defaults_to_full_refund():
    d = evaluate_refund(Policy(), pay(), [], None, "INR", "valid reason here", 0)
    assert d.verdict == "deny" and "FULL payment amount" in d.reasons[0]


def test_counts_pending_refunds_against_remaining():
    refunds = [{"amount": 6000, "status": "pending"}, {"amount": 3000, "status": "failed"}]
    d = evaluate_refund(Policy(), pay(), refunds, 5000, "INR", "valid reason here", 0)
    assert d.verdict == "deny" and d.remaining_refundable == 4000


def test_unconfigured_currency_is_refused():
    d = evaluate_refund(Policy(), pay(currency="EUR"), [], 500, "EUR", "valid reason here", 0)
    assert d.verdict == "deny" and any("EUR" in r for r in d.reasons)


def test_threshold_and_approval():
    p = pay(amount=900000, amount_received=900000)
    assert evaluate_refund(Policy(), p, [], 600000, "INR", "valid reason here", 0).verdict == "needs_approval"
    assert evaluate_refund(Policy(), p, [], 600000, "INR", "valid reason here", 0,
                           approval_granted=True).verdict == "allow"


def test_short_reason_and_min_amount():
    d = evaluate_refund(Policy(), pay(), [], 50, "INR", "x", 0)
    assert d.verdict == "deny" and len(d.reasons) == 2


# ---------- redaction ----------

def test_redaction_is_allow_list():
    fake = FakeHyperswitch()
    raw = dict(fake.payments["pay_demo_001"], brand_new_pii_field="aadhaar 1234")
    out = project_payment(raw)
    blob = json.dumps(out)
    for secret in ("asha.k@example.com", "9876543210", "4242", "secret_9f8a7b", "aadhaar", "MG Road", "cus_88"):
        assert secret not in blob
    assert "brand_new_pii_field" in out["redacted_fields"]
    assert out["customer_ref"].startswith("ref_")


# ---------- guard ----------

def test_retry_does_not_double_refund(env):
    fake, guard, _ = env
    a = guard.create_refund("pay_demo_001", 50000, "INR", "duplicate charge on A-1001")
    b = guard.create_refund("pay_demo_001", 50000, "INR", "duplicate charge on A-1001")
    assert a["status"] == "refunded" and b["status"] == "already_refunded"
    assert fake.create_calls == 1


def test_distinct_idempotency_keys_allow_two_partial_refunds(env):
    fake, guard, _ = env
    for key in ("item-1", "item-2"):
        assert guard.create_refund("pay_demo_001", 50000, "INR", "returned item", key)["status"] == "refunded"
    assert fake.create_calls == 2


def test_large_refund_queues_and_agent_retry_stays_queued(env):
    fake, guard, _ = env
    r = guard.create_refund("pay_demo_002", 800000, "INR", "order returned in full")
    again = guard.create_refund("pay_demo_002", 800000, "INR", "order returned in full")
    assert r["status"] == again["status"] == "pending_human_approval"
    assert fake.create_calls == 0 and list(guard.pending()) == [r["approval_id"]]
    done = guard.approve(r["approval_id"], "ops")
    assert done["status"] == "refunded" and fake.create_calls == 1 and guard.pending() == {}


def test_approval_rechecks_state(env):
    fake, guard, _ = env
    r = guard.create_refund("pay_demo_002", 800000, "INR", "order returned in full")
    # meanwhile someone refunds most of it from the dashboard
    fake.refunds["ref_dash"] = {"refund_id": "ref_dash", "payment_id": "pay_demo_002", "amount": 1000000,
                                "currency": "INR", "status": "succeeded"}
    out = guard.approve(r["approval_id"], "ops")
    assert out["status"] == "denied" and fake.create_calls == 0


def test_reject_removes_pending(env):
    _, guard, _ = env
    r = guard.create_refund("pay_demo_002", 800000, "INR", "order returned in full")
    assert guard.reject(r["approval_id"], "ops")["status"] == "rejected"
    assert guard.pending() == {}


def test_daily_budget_resets_next_day(env):
    _, guard, clock = env
    assert guard.create_refund("pay_demo_002", 500000, "INR", "return batch one")["status"] == "refunded"
    assert guard.create_refund("pay_demo_002", 500000, "INR", "return batch two")["status"] == "refunded"
    assert guard.create_refund("pay_demo_002", 100000, "INR", "return batch three")["status"] == "denied"
    clock["now"] = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    assert guard.create_refund("pay_demo_002", 100000, "INR", "return batch three")["status"] == "refunded"


def test_unknown_payment_and_bad_key(env, tmp_path):
    _, guard, _ = env
    assert guard.create_refund("pay_nope", 1000, "INR", "valid reason here")["status"] == "error"
    bad = Guard(HyperswitchClient("wrong", transport=FakeHyperswitch().transport()), Policy(), tmp_path / "b")
    assert bad.get_payment("pay_demo_001")["status"] == 401


# ---------- audit ----------

def test_audit_chain_detects_edit_and_delete(env):
    _, guard, _ = env
    guard.get_payment("pay_demo_001")
    guard.create_refund("pay_demo_001", None, "INR", "valid reason here")
    guard.create_refund("pay_demo_001", 50000, "INR", "duplicate charge on A-1001")
    assert guard.audit.verify()[0]
    lines = guard.audit.path.read_text().splitlines()
    assert [json.loads(x)["outcome"] for x in lines] == ["ok", "denied", "refunded"]
    guard.audit.path.write_text("\n".join([lines[0], lines[2]]) + "\n")  # delete the denial
    assert guard.audit.verify() == (False, "chain broken at entry 2")


# ---------- MCP ----------

def test_mcp_tools_in_memory(env):
    from mcp.client.client import Client
    from hsguard.server import build_server
    _, guard, _ = env

    async def go():
        async with Client(build_server(guard)) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) == {"get_payment", "list_payments", "list_refunds", "preview_refund", "create_refund"}
            assert tools["create_refund"].annotations.destructive_hint is True
            assert tools["get_payment"].annotations.read_only_hint is True
            assert "approve" not in " ".join(tools)  # agents get no approval tool
            res = await c.call_tool("create_refund", {"payment_id": "pay_demo_001", "currency": "INR",
                                                      "reason": "customer asked for refund"})
            assert not res.is_error
            return _result(res)
    out = asyncio.run(go())
    assert out["status"] == "denied"


def test_mcp_stdio_server_process(tmp_path):
    """Spawn `hsguard serve --mock` as a real stdio MCP server and call a tool over the pipe."""
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "hsguard.cli", "serve", "--mock"],
                                   env={"HSGUARD_STATE_DIR": str(tmp_path / "s"), "PATH": ""})

    async def go():
        async with Client(params) as c:
            res = await c.call_tool("preview_refund", {"payment_id": "pay_demo_001", "amount": 50000,
                                                       "currency": "INR", "reason": "duplicate charge"})
            assert not res.is_error
            return _result(res)
    out = asyncio.run(go())
    assert out["verdict"] == "allow" and out["dry_run"] is True
    assert AuditLog(tmp_path / "s" / "audit.jsonl").verify()[0]


def test_cli_refuses_live_url(monkeypatch):
    from hsguard import cli
    monkeypatch.setenv("HYPERSWITCH_API_KEY", "k")
    monkeypatch.setenv("HYPERSWITCH_BASE_URL", "https://api.hyperswitch.io")
    monkeypatch.delenv("HSGUARD_ALLOW_LIVE", raising=False)
    with pytest.raises(SystemExit, match="refusing non-sandbox"):
        cli.main(["pending"])


def test_demo_summary(tmp_path, capsys):
    from hsguard.demo import run_demo
    s = asyncio.run(run_demo(str(tmp_path)))
    assert s == {"agent_refund_requests": 8, "executed": 2, "denied": 5, "queued_for_human": 1,
                 "duplicate_requests_sent_on_retry": 0, "hyperswitch_refund_calls": 2,
                 "audit_ok": True, "tamper_detected": True}
