"""The check pipeline's result types and rendering.

Every phase appends `Check`s to one `Report`; the CLI renders the report as
text or JSON. Shared by all three phases (static / runtime / mini-game) so the
gate speaks one vocabulary regardless of which phase produced a finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    # NOTE is an actionable recommendation that never fails the run; safe or
    # expected states are PASS, not NOTE.
    NOTE = "note"
    SKIP = "skip"


@dataclass
class Check:
    group: str
    name: str
    status: Status
    detail: str = ""


@dataclass
class Report:
    challenge: str
    checks: list[Check] = field(default_factory=list)
    located: bool = True

    def add(self, group: str, name: str, status: Status, detail: str = "") -> Check:
        c = Check(group, name, status, detail)
        self.checks.append(c)
        return c

    @property
    def ok(self) -> bool:
        return self.located and not any(c.status is Status.FAIL for c in self.checks)


_SYMBOL = {Status.PASS: "✓", Status.FAIL: "✗", Status.NOTE: "–", Status.SKIP: "·"}


def render_text(rep: Report) -> str:
    lines = [f"challenge: {rep.challenge}"]
    last_group = None
    for c in rep.checks:
        if c.group != last_group:
            lines.append(f"  {c.group}")
            last_group = c.group
        detail = f"  — {c.detail}" if c.detail else ""
        lines.append(f"    {_SYMBOL[c.status]} {c.name}{detail}")
    n_fail = sum(1 for c in rep.checks if c.status is Status.FAIL)
    n_note = sum(1 for c in rep.checks if c.status is Status.NOTE)
    verdict = "PASS" if rep.ok else "FAIL"
    lines.append(f"  {verdict} — {n_fail} error(s), {n_note} note(s)")
    return "\n".join(lines)


def as_dict(rep: Report) -> dict:
    return {
        "challenge": rep.challenge,
        "ok": rep.ok,
        "checks": [
            {"group": c.group, "name": c.name, "status": c.status.value, "detail": c.detail}
            for c in rep.checks
        ],
    }
