"""The `python -m challenges.check` entry point.

One command, phases as additive flags:

    python -m challenges.check <name>            # Phase 1 (static, no Docker)
    python -m challenges.check <name> --build    # + Phase 2 (one live vulbox)
    python -m challenges.check <name> --game      # + Phase 3 (async mini-game)
    python -m challenges.check --all              # every challenge in the tree

`--build` and `--game` stack; `--all` applies the selected phases to each
challenge. A `FAIL` exits non-zero; a `NOTE` does not.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

from .constants import READY_TIMEOUT_DEFAULT
from .engine import run_checks
from .report import as_dict, render_text
from .resolve import discover_all


@contextlib.contextmanager
def _stdout_to_stderr():
    """Redirect fd 1 -> fd 2 for the duration. Challenge code (pwntools progress,
    debug prints) writes to stdout; without this it interleaves with — and
    corrupts — the report we emit on stdout (notably --json). fd-level so it also
    catches C/subprocess writes, not just Python prints."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="challenge-check",
        description="Readiness gate for a challenge — run the phases as flags.",
    )
    ap.add_argument("target", nargs="?", help="challenge name or challenges/<dir>")
    ap.add_argument("--all", action="store_true", help="check every challenge in the tree")
    ap.add_argument("--build", action="store_true",
                    help="Phase 2: wiring + one live container (Docker)")
    ap.add_argument("--game", action="store_true",
                    help="Phase 3: async two-team mini-game, including available "
                         "packaged PoV proof (Docker)")
    ap.add_argument("--allow-network", action="store_true",
                    help="run the --build container on a normal bridge (internet egress) "
                         "instead of the default internal bridge (host-reachable, no egress)")
    ap.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT_DEFAULT,
                    help="seconds to wait for the service to come up in --build "
                         f"(default {READY_TIMEOUT_DEFAULT:.0f})")
    ap.add_argument("--api-port", type=int, default=None,
                    help="--game: port for the orchestrator API (default: a free port)")
    ap.add_argument("--keep", action="store_true",
                    help="--game: keep the generated config + run dir instead of cleaning up")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="--game: stream the match's stdout/stderr instead of capturing it")
    ap.add_argument("--sync", action="store_true",
                    help="--game: run a strict DEFENSE→ATTACK match (investigation only; "
                         "the gate is async)")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args(argv)

    if not args.all and not args.target:
        ap.error("provide a challenge name/path, or --all")
    if args.ready_timeout <= 0:
        ap.error("--ready-timeout must be positive")

    targets = discover_all() if args.all else [args.target]
    if not targets:
        # all([]) is True — don't let "checked nothing" masquerade as success.
        print("no challenges found under challenges/", file=sys.stderr)
        return 1

    with _stdout_to_stderr():
        reports = [
            run_checks(
                t, build=args.build, game=args.game,
                allow_network=args.allow_network, ready_timeout=args.ready_timeout,
                api_port=args.api_port, keep=args.keep, verbose=args.verbose,
                mode="sync" if args.sync else "async",
            )
            for t in targets
        ]

    if args.json:
        import json
        payload = [as_dict(r) for r in reports]
        print(json.dumps(payload if args.all else payload[0], indent=2))
    else:
        print("\n\n".join(render_text(r) for r in reports))

    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
