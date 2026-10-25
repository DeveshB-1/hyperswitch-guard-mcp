"""MCP server exposing the guarded Hyperswitch tools to an AI agent."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .guard import Guard

INSTRUCTIONS = (
    "Tools for looking up Hyperswitch payments and issuing refunds under a merchant policy. "
    "Amounts are integers in minor units (paisa for INR, cents for USD). Always pass amount and "
    "currency explicitly. Call preview_refund before create_refund. A 'pending_human_approval' "
    "result is final for you: tell the user a human must approve it."
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True)


def build_server(guard: Guard) -> MCPServer:
    mcp = MCPServer(name="hyperswitch-guard", instructions=INSTRUCTIONS, version="0.1.0")

    @mcp.tool(annotations=READ)
    def get_payment(payment_id: str) -> dict:
        """Fetch one payment (PII fields such as email, phone, card and client_secret are removed)."""
        return guard.get_payment(payment_id)

    @mcp.tool(annotations=READ)
    def list_payments(limit: int = 10) -> dict:
        """List recent payments, newest first. limit is capped at 50."""
        return {"payments": guard.list_payments(limit)}

    @mcp.tool(annotations=READ)
    def list_refunds(payment_id: str) -> dict:
        """List refunds already issued against a payment."""
        return {"refunds": guard.list_refunds(payment_id)}

    @mcp.tool(annotations=READ)
    def preview_refund(payment_id: str, amount: int, currency: str, reason: str) -> dict:
        """Dry run: report whether a refund would be allowed, queued for a human, or denied, and why."""
        return guard.preview_refund(payment_id, amount, currency, reason)

    @mcp.tool(annotations=WRITE)
    def create_refund(payment_id: str, currency: str, reason: str, amount: int | None = None,
                      idempotency_key: str | None = None) -> dict:
        """Issue a refund through Hyperswitch if policy allows.

        Retrying the same payment_id + amount + idempotency_key (or reason) never refunds twice.
        Refunds above the auto-approve limit are queued for a human and NOT executed.
        """
        return guard.create_refund(payment_id, amount, currency, reason, idempotency_key)

    return mcp
