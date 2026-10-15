"""Constants shared by the static and runtime phases.

Game-phase timings (round length, attack budget, …) live in `game_phase.py`
since nothing else needs them.
"""

from __future__ import annotations

import re
from pathlib import Path

# The framework `challenges/` package dir (holds interface/, check/, registry).
# Challenges themselves NO LONGER live directly under here — they're in the
# selected challenge warehouse (challenge-warehouse/<contributor>/<challenge>/)
# and surfaced under `challenges.<name>` via challenges.__path__. So a challenge's
# real directory must be resolved through its package (challenge_dir), not
# `CHALLENGES_DIR / <name>`. CHALLENGES_DIR is still right for repo-relative
# lookups like `CHALLENGES_DIR.parent / "config"`.
CHALLENGES_DIR = Path(__file__).resolve().parent.parent


def challenge_dir(snake: str) -> Path | None:
    """Filesystem directory of the `challenges.<snake>` package, wherever it
    physically lives (a contributor folder inside the selected warehouse).
    Returns None if the package can't be located.

    Delegates to `registry.package_dir` (the canonical importlib resolver, which
    also runs inside the lean facility images); this authoring-tooling wrapper
    just keeps the historical name. Local import so `registry` — not `constants`
    — owns the logic without a load-order cycle."""
    from challenges.registry import package_dir
    return package_dir(snake)

# Files a contributed challenge directory must ship (see CONTRIBUTING.md).
# functionality_test.py and flag_handler.py must be their OWN modules (not
# inlined into challenge.py): the v3 health-poller / flag-service containers
# import them directly via registry.load_probe, which requires exactly one
# concrete probe class defined in each. Phase 1 checks they EXIST (a fast, clear
# diagnostic); Phase 3's facility import enforces the one-concrete-class rule.
REQUIRED_FILES = (
    "__init__.py",
    "challenge.py",
    "prompts.py",
    "build.py",
    "functionality_test.py",
    "flag_handler.py",
    "image/Dockerfile",
    "image/entrypoint.sh",
    "image/restart.sh",
    # NOTE: image/checker.sh is NO LONGER required. The CHECKER moved out of the
    # prod trust boundary — a challenge provides EITHER an optional `checker.py`
    # (network probe run in the poller, preferred) OR an optional
    # `image/checker.sh` (run over stdin in prod), OR neither. Presence is a soft
    # advisory in static_phase.check_checker, not a hard structural requirement.
)

# lowercase words joined by hyphens, e.g. nautilus-ico
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Upper bound on the --build readiness wait. await_ready returns as soon as the
# service is up, so this only costs time when a service is slow or broken — set
# with margin for a heavy first/cold boot (e.g. mlflow declares
# health_interval_secs=30 and can take a while to bind on a cold container).
READY_TIMEOUT_DEFAULT = 150.0
