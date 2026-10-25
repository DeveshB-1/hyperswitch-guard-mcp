"""Append-only, hash-chained JSONL audit log of every tool call the agent makes.

Each line carries the SHA-256 of the previous line's hash plus its own content,
so deleting or editing an entry breaks verification from that point on.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _digest(prev: str, body: dict) -> str:
    raw = prev + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _tail(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open() as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        e = json.loads(last)
        return e["seq"], e["hash"]

    def append(self, event: dict, now: datetime | None = None) -> dict:
        seq, prev = self._tail()
        body = {"seq": seq + 1, "ts": (now or datetime.now(timezone.utc)).isoformat(),
                "prev": prev, **event}
        entry = {**body, "hash": _digest(prev, body)}
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, e in enumerate(self.entries(), start=1):
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("prev") != prev or e.get("seq") != i or _digest(prev, body) != e.get("hash"):
                return False, f"chain broken at entry {i}"
            prev = e["hash"]
        return True, f"ok: {len(self.entries())} entries, head {prev[:12]}"
