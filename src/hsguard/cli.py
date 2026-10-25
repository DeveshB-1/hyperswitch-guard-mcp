"""hsguard: run the MCP server, or act as the human approver."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from .client import SANDBOX_URL, HyperswitchClient
from .guard import Guard
from .policy import Policy


def _guard(use_mock: bool) -> Guard:
    state = os.environ.get("HSGUARD_STATE_DIR", ".hsguard")
    policy = Policy.load(os.environ.get("HSGUARD_POLICY"))
    if use_mock:
        from .mock import MOCK_API_KEY, FakeHyperswitch
        return Guard(HyperswitchClient(MOCK_API_KEY, transport=FakeHyperswitch().transport()), policy, state)
    key = os.environ.get("HYPERSWITCH_API_KEY")
    if not key:
        sys.exit("HYPERSWITCH_API_KEY is not set (or pass --mock)")
    base = os.environ.get("HYPERSWITCH_BASE_URL", SANDBOX_URL)
    if "sandbox" not in base and os.environ.get("HSGUARD_ALLOW_LIVE") != "1":
        sys.exit(f"refusing non-sandbox base URL {base}; set HSGUARD_ALLOW_LIVE=1 if you mean it")
    return Guard(HyperswitchClient(key, base_url=base), policy, state)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="hsguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the MCP server over stdio")
    s.add_argument("--mock", action="store_true", help="use the in-memory Hyperswitch fixture")
    sub.add_parser("demo", help="run the scripted agent session against the mock")
    sub.add_parser("pending", help="list refunds waiting for a human")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("approval_id")
        p.add_argument("--by", default=os.environ.get("USER", "unknown"))
    sub.add_parser("audit-verify", help="check the audit log hash chain")
    a = ap.parse_args(argv)

    if a.cmd == "demo":
        from .demo import run_demo
        asyncio.run(run_demo())
        return
    if a.cmd == "audit-verify":
        from .audit import AuditLog
        ok, msg = AuditLog(os.path.join(os.environ.get("HSGUARD_STATE_DIR", ".hsguard"), "audit.jsonl")).verify()
        print(msg)
        sys.exit(0 if ok else 1)

    guard = _guard(getattr(a, "mock", False))
    if a.cmd == "serve":
        from .server import build_server
        build_server(guard).run("stdio")
    elif a.cmd == "pending":
        print(json.dumps(guard.pending(), indent=2))
    elif a.cmd == "approve":
        print(json.dumps(guard.approve(a.approval_id, a.by), indent=2))
    elif a.cmd == "reject":
        print(json.dumps(guard.reject(a.approval_id, a.by), indent=2))


if __name__ == "__main__":
    main()
