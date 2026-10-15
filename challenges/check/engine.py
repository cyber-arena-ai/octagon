"""Orchestration — resolve a challenge once, then run the selected phases.

`check_one` covers the in-process phases (static, and runtime under --build);
the mini-game (Phase 3) is driven separately by `game_phase.run_mini_game`
because it shells out to `main.py start`. `run_checks` composes them into one
report so the CLI can offer `--build` and `--game` as additive flags.
"""

from __future__ import annotations

from typing import Callable

from .constants import READY_TIMEOUT_DEFAULT
from .report import Report, Status
from . import resolve, runtime_phase, static_phase


def run_tier(rep: Report, group: str, fn: Callable[[], None]) -> None:
    """Run one check tier, turning an unexpected crash into a FAIL instead of
    letting it abort the whole run (and --all). Each tier handles its own
    EXPECTED errors; this is the backstop for the unexpected (e.g. a stale API
    call), mirroring the registry's skip-and-record philosophy."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — backstop; record, don't propagate
        rep.add(group, "crashed", Status.FAIL, repr(e))


def check_one(target: str, *, build: bool, allow_network: bool = False,
              ready_timeout: float = READY_TIMEOUT_DEFAULT) -> Report:
    """Phase 1 (static) and, with build=True, Phase 2 (runtime)."""
    snake, d, err = resolve.locate(target)
    if snake is None or d is None:
        rep = Report(challenge=target, located=False)
        rep.add("locate", "directory", Status.FAIL, err)
        return rep

    rep = Report(challenge=snake)
    static_phase.check_structure(d, rep)

    cls, note, note_status = resolve.load_challenge_class(snake)
    if cls is None:
        rep.add("import", "Challenge subclass", Status.FAIL, note)
        return rep
    if note:
        rep.add("import", "Challenge subclass", note_status, note)

    try:
        ch = cls()
        fresh = cls() is not ch
        rep.add("import", "instantiate", Status.PASS if fresh else Status.FAIL,
                "zero-arg, fresh instances" if fresh else "factory returns a shared instance")
    except Exception as e:
        rep.add("import", "instantiate", Status.FAIL, repr(e))
        return rep

    run_tier(rep, "interface", lambda: static_phase.check_interface(ch, rep))
    run_tier(rep, "probes", lambda: static_phase.check_probes(ch, rep))
    run_tier(rep, "dry-exec", lambda: static_phase.check_dry_exec(ch, rep))
    run_tier(rep, "checker", lambda: static_phase.check_checker(snake, d, ch, rep))
    run_tier(rep, "prompts/build", lambda: static_phase.check_prompts_build(snake, ch, rep))

    if build:
        run_tier(rep, "wiring", lambda: runtime_phase.check_wiring(snake, ch, rep))
        run_tier(rep, "build", lambda: runtime_phase.check_build_run(
            snake, ch, rep, allow_network=allow_network, ready_timeout=ready_timeout))

    return rep


def run_checks(target: str, *, build: bool = False, game: bool = False,
               allow_network: bool = False, ready_timeout: float = READY_TIMEOUT_DEFAULT,
               api_port: int | None = None, keep: bool = False, verbose: bool = False,
               mode: str = "async") -> Report:
    """Run the selected phases into one report. Phase 1 always runs; --build adds
    Phase 2; --game adds Phase 3 (the mini-game), appended to the same report."""
    rep = check_one(target, build=build, allow_network=allow_network,
                    ready_timeout=ready_timeout)
    if game:
        # Only run a live match once Phase 1 has located the challenge — the
        # mini-game's resolver is more permissive (it falls back to a package
        # dir name), so on a not-located target it could otherwise spin up a
        # Docker match for a different, same-named in-tree package. Wrap in
        # run_tier so a setup crash becomes a recorded FAIL, not an aborted run.
        if rep.located:
            from . import game_phase
            run_tier(rep, "run", lambda: game_phase.run_mini_game(
                target, rep=rep, api_port=api_port, keep=keep,
                verbose=verbose, mode=mode))
        else:
            rep.add("run", "mini-game", Status.SKIP,
                    "skipped — challenge not located in Phase 1")
    return rep
