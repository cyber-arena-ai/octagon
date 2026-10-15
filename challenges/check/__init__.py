"""Challenge readiness gate — run with ``python -m challenges.check``.

One command, phases as flags:

    python -m challenges.check <name>           # Phase 1: static package contract
    python -m challenges.check <name> --build   # + Phase 2: one live vulbox
    python -m challenges.check <name> --game     # + Phase 3: async mini-game

Internally: `report` is the shared result vocabulary; `resolve` locates the
challenge; `docker_env` / `source_scan` are utilities; `static_phase`,
`runtime_phase`, and `game_phase` are the three phases; `engine` composes them.
"""

from __future__ import annotations

from .engine import check_one, run_checks
from .game_phase import assert_events, build_game_config, run_mini_game
from .report import Check, Report, Status, as_dict, render_text

__all__ = [
    "Check",
    "Report",
    "Status",
    "as_dict",
    "render_text",
    "check_one",
    "run_checks",
    "run_mini_game",
    "build_game_config",
    "assert_events",
]
