"""Per-principal usage ledger + spend cap (in-memory, per-process).

Tracks calls / tokens / cost per principal and enforces an optional USD cap.
Cost is only known after a call returns, so the cap is a soft pre-check: if a
principal's accumulated spend is already at/over its cap the next request is
rejected; otherwise it's allowed and its usage is recorded afterwards. The
request that crosses the line completes — standard for token-metered budgets.

State is in-memory and resets on restart (one arena run = one process). A
principal with no configured cap is unlimited. `report()` powers the GET /usage
endpoint so spend is visible without parsing the JSONL log.
"""

from __future__ import annotations


def _empty() -> dict:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}


class Budget:
    def __init__(self, limits: dict[str, float] | None = None) -> None:
        # principal id -> USD cap. Absent ⇒ unlimited.
        self._limits = dict(limits or {})
        self._usage: dict[str, dict] = {}

    def exceeded(self, principal: str | None) -> bool:
        """True iff this principal has a cap and has already reached/passed it."""
        lim = self._limits.get(principal) if principal is not None else None
        return lim is not None and self.spent(principal) >= lim

    def record(self, principal: str | None, *, input_tokens: int = 0,
               output_tokens: int = 0, cost: float | None = 0.0) -> None:
        """Add one call's usage to the principal's running totals."""
        if principal is None:
            return
        row = self._usage.setdefault(principal, _empty())
        row["calls"] += 1
        row["input_tokens"] += int(input_tokens or 0)
        row["output_tokens"] += int(output_tokens or 0)
        row["cost_usd"] += float(cost or 0.0)

    def spent(self, principal: str | None) -> float:
        if principal is None:
            return 0.0
        return self._usage.get(principal, _empty())["cost_usd"]

    def limit(self, principal: str | None) -> float | None:
        return self._limits.get(principal) if principal is not None else None

    def report(self) -> list[dict]:
        """Per-principal usage snapshot (includes capped principals with no usage
        yet), sorted by id. Drives GET /usage."""
        rows = []
        for p in sorted(set(self._usage) | set(self._limits)):
            u = self._usage.get(p, _empty())
            lim = self._limits.get(p)
            rows.append({
                "principal": p,
                "calls": u["calls"],
                "input_tokens": u["input_tokens"],
                "output_tokens": u["output_tokens"],
                "cost_usd": round(u["cost_usd"], 6),
                "budget_usd": lim,
                "remaining_usd": None if lim is None else round(lim - u["cost_usd"], 6),
            })
        return rows
