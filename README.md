# hyperswitch-guard-mcp

An MCP server that lets an AI agent look up Hyperswitch payments and issue refunds, with a merchant-side policy layer between the agent and the API.

The agent gets 5 tools: `get_payment`, `list_payments`, `list_refunds`, `preview_refund`, `create_refund`. Every call goes through a guard that:

- **Refuses a refund with no amount.** The Hyperswitch `POST /refunds` spec says an omitted `amount` "will default to the full payment amount". An LLM that drops one argument would refund the whole order. The guard denies it and says why.
- **Makes retries safe.** The guard derives Hyperswitch's `refund_id` from `payment_id + amount + idempotency_key` (or the reason), so an agent that retries after a timeout gets `already_refunded` back and nothing new is sent.
- **Checks what's actually refundable**: payment status, `amount_received` minus succeeded, pending and review refunds, the request's currency against the payment's, Hyperswitch's 100-minor-unit minimum.
- **Applies money limits per currency**: an auto-approve limit, a hard limit, and a daily budget for all agent refunds (so 20 small refunds can't add up past the cap). A currency with no limits configured is refused.
- **Queues big refunds for a human.** Above the auto-approve limit the request lands in a pending queue. Approval happens only from the `hsguard approve` CLI. There is no MCP tool for it, so the agent can't approve its own request. On approval the guard re-reads the payment and re-checks, in case someone refunded it from the dashboard in the meantime.
- **Keeps PII away from the model.** Payment objects pass through an allow-list: email, phone, name, billing, card data, `client_secret` and the customer object never reach the LLM. `customer_id` becomes a stable pseudonym so the agent can still group by customer. A field added to the API later stays hidden until someone allow-lists it.
- **Writes a hash-chained audit log.** Every tool call, including denials, is one JSONL line chained by SHA-256. Editing or deleting a line breaks `hsguard audit-verify` from that entry on.

## Why this matters to Juspay

Hyperswitch already has an MCP endpoint (the Mintlify one at `api-reference.hyperswitch.io/mcp`). Its setup page says it's for docs search and sandbox exploration, that production keys must not be used with it, and that responses "can be incorrect". That fits a developer poking at the API.

Merchants who want an agent in their support or ops loop ("refund the duplicate charge on order A-1001") need something they'd trust with a real key: limits, a human gate, safe retries, no PII in the prompt, a record of what the agent did. This is a small, working take on that layer for Hyperswitch's refund flow. The same pattern extends to capture, cancel and payment links.

## Quickstart

```bash
uv sync
uv run hsguard demo            # scripted agent session against the in-memory fixture
uv run pytest -q               # 18 tests

# MCP server over stdio
uv run hsguard serve --mock    # fixture data, no key needed
HYPERSWITCH_API_KEY=snd_... uv run hsguard serve   # Hyperswitch sandbox

# human side
uv run hsguard pending
uv run hsguard approve <approval_id> --by you@merchant
uv run hsguard reject  <approval_id>
uv run hsguard audit-verify
```

Claude Code: `claude mcp add hyperswitch-guard -e HYPERSWITCH_API_KEY=snd_... -- uv run --directory /path/to/hyperswitch-guard-mcp hsguard serve`

Config (env): `HYPERSWITCH_API_KEY`, `HYPERSWITCH_BASE_URL` (default `https://sandbox.hyperswitch.io`; any non-sandbox URL is refused unless `HSGUARD_ALLOW_LIVE=1`), `HSGUARD_STATE_DIR` (default `.hsguard`), `HSGUARD_POLICY` (JSON file overriding the limits in `policy.py`).

Default limits (minor units): auto-approve INR 500000 / USD 5000, hard limit INR 2000000 / USD 50000, daily budget INR 1000000 / USD 20000.

## Demo run

`uv run hsguard demo` drives the server through a real MCP client (`tools/call` for every agent step). Output from my run, also in `examples/demo-output.txt`:

```
MCP tools exposed: create_refund, get_payment, list_payments, list_refunds, preview_refund

[agent] get_payment pay_demo_001
        -> succeeded Rs 2,500.00; hidden from the model: billing, client_secret, customer, customer_id, email, merchant_id, name, payment_method_data, phone
[agent] refund pay_demo_001, no amount given
        -> denied: amount is required: Hyperswitch refunds the FULL payment amount when it is omitted
[agent] refund Rs 500 on pay_demo_001
        -> refunded
[agent] same Rs 500 refund again (agent retry)
        -> already_refunded
[agent] refund Rs 2,500 on pay_demo_001 (only Rs 2,000 left)
        -> denied: amount 250000 exceeds remaining refundable 200000
[agent] refund 2000 'INR' on a USD payment
        -> denied: currency mismatch: request says 'INR', payment is 'USD'
[agent] refund an unpaid checkout
        -> denied: payment status 'requires_payment_method' is not refundable
[agent] refund Rs 8,000 on pay_demo_002
        -> pending_human_approval: amount 800000 is above the auto-approve limit 500000 INR; queued for a human
[human] hsguard approve agt_b22e0446a07edbdcef50a14e
        -> refunded
[agent] another Rs 3,000 refund the same day
        -> denied: daily agent refund budget exceeded: 850000 already used of 1000000 INR

Audit log: ok: 10 entries, head 75c4eae4e44b
Tampered copy: chain broken at entry 3

Summary: {"agent_refund_requests": 8, "executed": 2, "denied": 5, "queued_for_human": 1, "duplicate_requests_sent_on_retry": 0, "hyperswitch_refund_calls": 2, "audit_ok": true, "tamper_detected": true}
```

8 refund requests from the agent, 2 refunds reached Hyperswitch (one of them after a human approved it).

Tests: `18 passed in 1.55s`. They cover the policy rules, allow-list redaction (including a made-up new PII field), retry idempotency, the approve/reject path and its re-check, the daily budget rolling over at midnight UTC, audit tamper detection (edit and delete), the tools over an in-memory MCP client, and `hsguard serve --mock` spawned as a real stdio MCP process.

## What's mocked, what's real

- The demo and tests run against `hsguard/mock.py`, an in-memory stand-in for `GET /payments/{id}`, `GET /payments/list`, `POST /refunds`, `POST /refunds/list` and `GET /refunds/{id}`. Field names, the full-amount default and the 100 minimum come from Hyperswitch's published OpenAPI spec (`api-reference/v1/openapi_spec_v1.json`).
- The real client points at `https://sandbox.hyperswitch.io` with the `api-key` header. I called the live sandbox with an invalid key and got `401 IR_01 "API key not provided or invalid API key used"`, the same shape the mock returns. I haven't run a full refund against the live sandbox (no merchant key), so that path is untested end to end.
- Local state (pending queue, executed ledger) is a JSON file. Fine for one operator; a shared deployment would want a database and a lock.

## Layout

```
src/hsguard/
  client.py   Hyperswitch REST client
  policy.py   refund rules, limits, decisions
  redact.py   allow-list projection of payments/refunds
  audit.py    hash-chained JSONL audit log
  guard.py    ties it together: idempotency, approval queue, ledger
  server.py   MCP tools (mcp Python SDK 2.x, MCPServer)
  cli.py      serve / demo / pending / approve / reject / audit-verify
  mock.py     in-memory Hyperswitch fixture
  demo.py     scripted agent session over MCP
tests/test_guard.py
```

Built by Devesh B (github.com/DeveshB-1). Not affiliated with Juspay.
