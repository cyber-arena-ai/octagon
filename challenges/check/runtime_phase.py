"""Phase 2 — single-vulbox runtime (needs Docker).

Cross-module wiring (auto-registry, prompt routing, game_*.yaml), then build one
image, start one container, and exercise the three functions the running game
calls — functionality_test, flag plant/retrieve, restart — plus the one check
the loopback probes can't see: a planted flag surviving a restart.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from challenges.interface import (
    Challenge,
    ObservationStatus,
    StateReadResult,
    StateReadStatus,
    VulboxTarget,
)

from .constants import CHALLENGES_DIR
from .docker_env import await_ready, leaf_summary, resolve_and_build_image
from .flag_checks import (
    CHECK_ROUND_CONTEXT_SEED,
    check_marks_absent,
    make_check_flags,
    make_check_marks,
    validate_handles,
)
from .report import Report, Status
from .source_scan import image_repo, image_tag


def check_wiring(snake: str, ch: Challenge, rep: Report) -> None:
    from challenges import registry

    g = "wiring"
    name = ch.name

    # discovered by the auto-registry?  build_challenge() runs the registry's
    # CLASS-level validation — exactly one concrete Challenge subclass, `name`
    # is a class attribute (not a @property), no duplicate name — none of which
    # the static interface phase can see (it reads ch.name off an instance). So
    # this is the check that catches a class-level mistake like a @property
    # `name`. Image building is the challenges.<pkg>.build.build_image()
    # convention (checked statically, and exercised for real below), so there
    # is no separate builder registration left to verify.
    try:
        built = registry.build_challenge(name)
        ok = isinstance(built, type(ch))
        rep.add(g, "registry discovery",
                Status.PASS if ok else Status.FAIL,
                "discovered" if ok else
                f"build_challenge({name!r}) -> {type(built).__name__}, expected {type(ch).__name__}")
    except KeyError:
        rep.add(g, "registry discovery", Status.FAIL,
                f"{name!r} not discovered — check the class-level contract "
                f"(one concrete Challenge subclass; `name` a class attribute)")
    except Exception as e:
        # Discovery itself aborted (e.g. another challenge fails class-level
        # validation) — a real gate failure, surfaced rather than swallowed.
        rep.add(g, "registry discovery", Status.FAIL, f"auto-discovery raised {e!r}")

    # Prompt module — resolved by convention from the discovered package.
    try:
        import importlib

        want = f"challenges.{snake}.prompts"
        package = registry.challenge_package(name)
        got = f"challenges.{package}.prompts"
        module = importlib.import_module(got)
        if got == want and callable(getattr(module, "build_advanced_prompt", None)):
            rep.add(g, "prompt auto-discovery", Status.PASS, want)
        elif got != want:
            rep.add(g, "prompt auto-discovery", Status.FAIL,
                    f"resolved {got!r}, expected {want!r}")
        else:
            rep.add(g, "prompt auto-discovery", Status.FAIL,
                    f"{got} has no callable build_advanced_prompt")
    except Exception as e:
        rep.add(g, "prompt auto-discovery", Status.FAIL, repr(e))

    # game.yaml wiring — parse the configs that reference this challenge and
    # confirm it's actually deployable (port / deploy_on teams / prod image).
    _check_config_wiring(name, ch, rep)


def _load_game_config(path: Path):
    """Load a game_*.yaml for the wiring checks, tolerating an unpopulated .env.

    gamemgr.config.load() hard-fails when any ${VAR} the file interpolates is
    unset — the normal case when the checker runs without a populated .env. The
    fields we actually verify (port / deploy_on / prod image) are plain literals,
    never ${VAR}-interpolated, so a missing-secret failure would otherwise skip
    the deployability checks and silently let a real misconfig pass. Fill any
    unset referenced vars with a placeholder just for the load, then restore
    os.environ."""
    import os
    from gamemgr.config import load

    referenced = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)\}", path.read_text()))
    added = [v for v in referenced if v not in os.environ]
    for v in added:
        os.environ[v] = "check-placeholder"
    try:
        return load(str(path))
    finally:
        for v in added:
            os.environ.pop(v, None)


def _check_config_wiring(name: str, ch: Challenge, rep: Report) -> None:
    """Verify a game_*.yaml deploys the challenge consistently with its vulbox.

    Advisory when nothing references it (the contributor hasn't wired it yet);
    a hard FAIL when a referencing config is misconfigured (wrong port, unknown
    deploy_on team, or a prod node whose image doesn't match the vulbox).
    """
    g = "wiring"
    cfg_dir = CHALLENGES_DIR.parent / "config"
    if not cfg_dir.is_dir():
        rep.add(g, "config wiring", Status.NOTE, "no config/ dir")
        return

    # Cheap pre-filter: only parse configs that actually name this challenge.
    # Anchored so `vulbox_name:` doesn't match and `foo` doesn't match `foo-extra`.
    pat = re.compile(rf"(?m)^\s*-?\s*name:\s*[\"']?{re.escape(name)}[\"']?\s*$")
    candidates = []
    for y in sorted(cfg_dir.glob("*.yaml")):
        try:
            if pat.search(y.read_text()):
                candidates.append(y)
        except Exception:
            pass
    if not candidates:
        rep.add(g, "config wiring", Status.NOTE,
                "no game_*.yaml references this challenge yet")
        return

    try:
        import gamemgr.config  # noqa: F401 — confirm the loader is importable
    except Exception as e:
        rep.add(g, "config wiring", Status.NOTE, f"cannot import gamemgr.config: {e!r}")
        return

    wired_ok = []
    for y in candidates:
        try:
            cfg = _load_game_config(y)
        except Exception as e:
            rep.add(g, f"config {y.name}", Status.NOTE, f"parse failed: {e!r}")
            continue
        problems, image_issues = verify_config_wiring(cfg, name, ch)
        if problems is None:
            continue  # text matched a non-challenge `name:` line
        if problems:
            rep.add(g, f"config {y.name}", Status.FAIL, "; ".join(problems))
        elif image_issues:
            rep.add(g, f"config {y.name}", Status.NOTE, "; ".join(image_issues))
            wired_ok.append(y.name)
        else:
            wired_ok.append(y.name)

    if wired_ok:
        rep.add(g, "config wiring", Status.PASS, ", ".join(wired_ok))
    elif not any(c.group == g and c.name.startswith("config ") for c in rep.checks):
        # candidates matched text but none had a real challenges entry
        rep.add(g, "config wiring", Status.NOTE,
                "referenced in a config but no matching challenges entry")


def verify_config_wiring(cfg, name: str, ch: Challenge):
    """Compare a parsed GameConfig against a challenge's vulbox.

    Returns ``(problems, image_issues)``. ``problems`` are deployability bugs
    that FAIL the gate (wrong port, unknown deploy_on team, missing prod node,
    a prod image in a *different repository* than the vulbox, or a same-repo
    tag drift). A tag drift FAILs because the --build run validates the builder
    image (``ch.vulbox.reference``) while the game deploys this pinned tag — so
    a stale/incompatible tag would ship code the gate never exercised.
    ``image_issues`` are advisory (NOTE): a tag drift on a *debug/smoke variant*
    config (it legitimately pins its own tag), or an unresolvable vulbox pin
    (reads as ``:unpinned`` without the vendor checkout, so it can't be
    compared). Returns ``(None, None)`` when the config has no ``challenges``
    entry for ``name`` (the pre-filter matched a stray ``name:``).
    """
    chcfg = next((c for c in cfg.challenges if c.name == name), None)
    if chcfg is None:
        return None, None

    ref = ch.vulbox.reference
    tag_resolved = not ref.endswith(":unpinned")
    exposed = set(ch.vulbox.exposed_ports.values())

    problems: list[str] = []
    image_issues: list[str] = []
    if chcfg.port not in exposed:
        problems.append(f"port {chcfg.port} not in exposed_ports {sorted(exposed)}")
    for team_id in chcfg.deploy_on:
        team = next((t for t in cfg.topology.teams if t.id == team_id), None)
        if team is None:
            problems.append(f"deploy_on team {team_id!r} not in topology")
            continue
        try:
            prod = cfg.topology.node(team.prod)
        except Exception:
            problems.append(f"team {team_id} prod node {team.prod!r} missing")
            continue
        if not tag_resolved:
            image_issues.append(f"team {team_id} image tag unverified (vulbox pin unresolved)")
        elif prod.image != ref:
            # A different repository means the config deploys an unrelated image
            # — a genuine misconfiguration that FAILs.
            if image_repo(prod.image) != image_repo(ref):
                problems.append(
                    f"team {team_id} prod image {prod.image!r} is a different image "
                    f"than vulbox {ref!r}")
            else:
                # Same repo. Compare the parsed tags. A digest pin (repo@sha256:)
                # or an untagged ref has no tag to compare, so drift can't be
                # judged — advisory NOTE, not a false "stale tag" FAIL.
                prod_tag, ref_tag = image_tag(prod.image), image_tag(ref)
                if not prod_tag or not ref_tag:
                    image_issues.append(
                        f"team {team_id} prod image {prod.image!r} vs vulbox {ref!r}: "
                        f"tag drift unverifiable (digest or untagged pin)")
                # An intentional debug/smoke variant pins its own tag — match the
                # tag exactly (or a -smoke/-debug suffix), NOT any substring, so a
                # tag like ':v2-debugsymbols' isn't mistaken for a variant.
                elif prod_tag in ("smoke", "debug") or prod_tag.endswith(("-smoke", "-debug")):
                    image_issues.append(
                        f"team {team_id} prod image {prod.image!r} != vulbox {ref!r} "
                        f"(debug/smoke variant — not validated by this run)")
                else:
                    # Same repo, different real tag: the --build run validated
                    # `ref`, but this config would deploy `prod.image` —
                    # stale/unvalidated code.
                    problems.append(
                        f"team {team_id} prod image {prod.image!r} != vulbox {ref!r} — the "
                        f"gate built and ran {ref!r}; this config deploys a different tag "
                        f"(stale/unvalidated code). Re-pin the config or vulbox.reference.")
    return problems, image_issues


def check_build_run(snake: str, ch: Challenge, rep: Report, *,
                    allow_network: bool, ready_timeout: float) -> None:
    g = "build"
    try:
        import docker  # noqa: F401
        from orchestrator.infra.docker import ContainerManager
    except Exception as e:
        rep.add(g, "docker", Status.FAIL, f"docker SDK unavailable: {e!r}")
        return

    cm = ContainerManager()
    client = cm._client

    # build / resolve image
    try:
        tag = resolve_and_build_image(snake, ch, client)
        rep.add(g, "image build", Status.PASS, tag)
    except Exception as e:
        rep.add(g, "image build", Status.FAIL, repr(e))
        return

    cname = f"cyberarena-check-{snake}"
    netname = f"cyberarena-check-net-{snake}"
    cm.remove(cname)
    # Network: default to an INTERNAL bridge, not `network=none`. The container
    # then has a host-reachable IP, so a challenge whose probe/flag-handler
    # resolves the container IP (docker inspect) and connects from the
    # orchestrator process works — exactly like the real game, where the host
    # sits on every team bridge and reaches each vulbox by IP. An internal
    # bridge still blocks internet egress, so "self-contained startup, nothing
    # fetched after build" stays enforced. `network=none` was STRICTER than the
    # real deploy (no IP at all) and false-failed every host-side-probe
    # challenge. exec_in_container probes (127.0.0.1) work on either network.
    # --allow-network swaps in a normal bridge for the rare challenge that
    # legitimately needs egress at startup.
    net = None
    run_kwargs: dict[str, Any] = dict(name=cname, detach=True, hostname=cname)
    if not allow_network:
        try:
            client.networks.get(netname).remove()  # clear a stale leftover
        except Exception:
            pass
        net = client.networks.create(netname, driver="bridge", internal=True)
        run_kwargs["network"] = netname

    def _drop_net() -> None:
        if net is not None:
            try:
                net.remove()
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass

    try:
        container = client.containers.run(tag, **run_kwargs)
    except Exception as e:
        rep.add(g, "container start", Status.FAIL, repr(e))
        cm.remove(cname)
        _drop_net()
        return

    # Reuse ContainerManager.exec (get + exec_run + decode) rather than
    # reimplementing it; the `bash -c` wrap matches how GameMgr drives probes.
    def exec_in(host: str, cmd: str) -> tuple[int, str]:
        return cm.exec(host, ["bash", "-c", cmd])

    # Stand in for the facility's authoritative-state capability so a WRITE
    # handler that reads through it (instead of through prod) is exercised here
    # exactly as the live flag-service exercises it.
    state_regions = {r.name: r for r in ch.authoritative_state_regions()}

    def read_state(region_name: str) -> StateReadResult:
        region = state_regions.get(region_name)
        if region is None:
            return StateReadResult(
                StateReadStatus.ERROR, detail="unknown authoritative-state region"
            )
        try:
            files = cm.read_files(cname, region.path, max_bytes=region.max_bytes)
            return StateReadResult(StateReadStatus.OK, files=files)
        except Exception:
            return StateReadResult(
                StateReadStatus.ERROR, detail="authoritative state unreadable"
            )

    target = VulboxTarget(
        host=cname,
        ports=dict(ch.vulbox.exposed_ports),
        meta={
            "team_id": "check",
            "exec_in_container": exec_in,
            "read_authoritative_state": read_state,
        },
    )

    try:
        # FAIL only if the container actually died — not on the brief
        # created/starting window right after a detached run (that's a race the
        # readiness poll below tolerates). Capture logs so a boot crash is
        # diagnosable before the finally removes the container.
        container.reload()
        if container.status in ("exited", "dead"):
            logs = ""
            try:
                logs = container.logs(tail=20).decode(errors="replace").strip()
            except Exception:
                pass
            rep.add(g, "container start", Status.FAIL,
                    f"container {container.status} on boot; logs: {logs[-300:]}")
            return
        rep.add(g, "container start", Status.PASS,
                "internal bridge (host-reachable, no egress)" if not allow_network
                else "bridge network (egress allowed)")

        # post-deploy first-start hook (spawns the app for ssh-only entrypoints)
        try:
            ch.initial_start(target, exec_in)
            rep.add(g, "initial_start", Status.PASS, "")
        except Exception as e:
            rep.add(g, "initial_start", Status.FAIL, repr(e))

        # Mirror the game's bring-up order: deploy waits for the service PORT,
        # THEN plant_initial_flags, THEN the health poll (main.py:176-183). The
        # flag must exist BEFORE the functionality probes — a checker that reads
        # the flag on each connection (e.g. nautilus ico reads /flag) resets every
        # probe until it does, a false "service down" the real run never sees.
        # _await_service_port is the same flag-independent primitive initial_start
        # uses; gating plant on it gives a network-based plant a bound port
        # (idempotent for the RestartScript challenges that already waited).
        from challenges.interface.challenge import _await_service_port
        _await_service_port(ch.name, target, exec_in)

        # 1) flag_handler.plant — planted before readiness, like the game. Handles
        # are kept for the round-trip and post-restart survival checks below.
        flags = {}
        handles: dict[str, str] = {}
        plant_err: Exception | None = None
        try:
            flags = make_check_flags(ch)
            plant_target = VulboxTarget(
                host=target.host,
                ports=dict(target.ports),
                meta={
                    **target.meta,
                    "round_context_seed": CHECK_ROUND_CONTEXT_SEED,
                },
            )
            raw_handles = ch.flag_handler.plant(plant_target, flags)
            handles_ok, detail = validate_handles(flags, raw_handles)
            if not handles_ok:
                raise ValueError(detail)
            handles = dict(raw_handles)
        except Exception as e:
            plant_err = e

        # readiness — wait for the service to actually come up (flag present now)
        ready, detail = await_ready(ch, target, ready_timeout)
        rep.add(g, "service readiness",
                Status.PASS if ready else Status.FAIL,
                f"up in <={int(ready_timeout)}s" if ready else detail)

        # 2) functionality_test.run — the health poller's call
        try:
            r = ch.functionality_test.run(target)
            rep.add(g, "functionality_test.run",
                    Status.PASS if r.passed else Status.FAIL, leaf_summary(r))
        except Exception as e:
            rep.add(g, "functionality_test.run", Status.FAIL, repr(e))

        # 3) flag plant -> retrieve round-trip, reported now that readiness holds.
        if plant_err is not None:
            rep.add(g, "flag plant->retrieve", Status.FAIL, f"plant raised {plant_err!r}")
        else:
            failures = []
            for store, flag in flags.items():
                try:
                    obs = ch.flag_handler.retrieve(
                        target, handles[store], expected=flag.value)
                    if obs.status is not ObservationStatus.PRESENT:
                        failures.append(
                            f"{store}: {obs.status.value} "
                            f"({obs.detail or obs.value!r})")
                except Exception as e:
                    failures.append(f"{store}: raised {e!r}")
            rep.add(g, "flag plant->retrieve",
                    Status.FAIL if failures else Status.PASS,
                    "; ".join(failures) if failures
                    else f"round-trips exact flags for stores {sorted(flags)}")

            # A WRITE declaration is not enough: prove the host-side reader can
            # actually snapshot every allowlisted region after plant has prepared
            # the target. This reads directly through the trusted Docker backend,
            # independent of whatever path the handler takes.
            if state_regions:
                state_failures = []
                for region_name in sorted(state_regions):
                    result = read_state(region_name)
                    if result.status is not StateReadStatus.OK:
                        state_failures.append(
                            f"{region_name}: {result.detail or result.status.value}"
                        )
                rep.add(
                    g, "authoritative state readable",
                    Status.FAIL if state_failures else Status.PASS,
                    "; ".join(state_failures) if state_failures
                    else f"host-read regions {sorted(state_regions)}",
                )

            # 3b) The claim half of retrieve(), for WRITE stores only: a mark
            # nobody placed must read back NOT_FOUND. Without this a handler that
            # always says PRESENT (credits everyone) or always ERRORs (credits
            # nobody) reaches a live match with a green review — the plant->
            # retrieve probe above only exercises the integrity-anchor half.
            marks = make_check_marks(ch)
            if marks:
                ok, detail = check_marks_absent(
                    ch.flag_handler, target, handles, marks,
                    require_authoritative_read=True)
                rep.add(g, "write mark absent",
                        Status.PASS if ok else Status.FAIL, detail)

        # 4) restart_handler.run — the MCP restart_service call
        if ch.restart_handler is None:
            # The interface phase already FAILs a None restart_handler; here we
            # just skip cleanly instead of dereferencing None.
            rep.add(g, "restart_handler.run", Status.SKIP,
                    "no restart_handler (defense disabled)")
        else:
            try:
                rr = ch.restart_handler.run(target)
                rep.add(g, "restart_handler.run",
                        Status.PASS if rr.passed else Status.FAIL, leaf_summary(rr))
            except Exception as e:
                rep.add(g, "restart_handler.run", Status.FAIL, repr(e))

        # post-restart health re-probe (runtime triggers this after a restart)
        try:
            r2 = ch.functionality_test.run(target)
            rep.add(g, "functionality_test (post-restart)",
                    Status.PASS if r2.passed else Status.FAIL, leaf_summary(r2))
        except Exception as e:
            rep.add(g, "functionality_test (post-restart)", Status.FAIL, repr(e))

        # 5) flag survives the restart. The game plants the flag at defense
        # entry and never re-plants after an agent's restart_service, so the
        # same handle must still resolve to the planted flag. A restart that
        # wipes /flag or resets the backing store would erase attackers'
        # targets while passing every check above.
        if ch.restart_handler is not None and handles:
            failures = []
            for store, flag in flags.items():
                try:
                    obs = ch.flag_handler.retrieve(
                        target, handles[store], expected=flag.value)
                    if obs.status is not ObservationStatus.PRESENT:
                        failures.append(
                            f"{store}: {obs.status.value} "
                            f"({obs.detail or obs.value!r})")
                except Exception as e:
                    failures.append(f"{store}: raised {e!r}")
            rep.add(g, "flag survives restart",
                    Status.FAIL if failures else Status.PASS,
                    "; ".join(failures) if failures
                    else f"flags intact after restart for stores {sorted(flags)}")

        # 6) negative health probe — does functionality_test actually go RED
        # when the service is broken? Without this the gate can't tell a faithful
        # checker from a no-op that always passes (README: "health checks fail
        # when the actual challenge logic is broken, not only when a process or
        # port is down"). Opt-in via the challenge's break_service hook; absent
        # is a NOTE, not a FAIL. Runs LAST so a broken (or partly-recovered)
        # service can't taint the positive checks above.
        _negative_health_probe(ch, target, exec_in, rep, g, ready_timeout=ready_timeout)
    finally:
        cm.remove(cname)
        _drop_net()


def _negative_health_probe(
    ch: Challenge, target: VulboxTarget,
    exec_in: Callable[[str, str], tuple[int, str]], rep: Report, g: str, *,
    ready_timeout: float,
) -> None:
    """Reliably take the live service DOWN, require functionality_test to report
    it as failing, then restart and require recovery.

    Gates one necessary, mechanical property: that the checker RETURNS a failing
    CheckResult when the service is down — so the real health poller turns it red.
    A checker that passes unconditionally, or that RAISES (which the real poller
    swallows, keeping the service green), would never flag a down service in-game.
    It needs the challenge to know how to take itself down reliably, so it is
    opt-in via `break_service`: a challenge that ships none gets a NOTE, never a
    FAIL. Subtle logic-level faithfulness (a broken-but-up service) stays the
    author's job.
    """
    try:
        broke = ch.break_service(target, exec_in)
    except Exception as e:
        rep.add(g, "negative health probe", Status.NOTE,
                f"break_service raised {e!r}; down-service detection unverified")
        return
    if broke is None:
        rep.add(g, "negative health probe", Status.NOTE,
                "no break_service hook — whether the checker reports a DOWN "
                "service as failing is unverified; add break_service (take the "
                "service down reliably) to gate it")
        return

    # Must RETURN passed=False; a raise is a FAIL (the real poller swallows it and stays green).
    try:
        r = ch.functionality_test.run(target)
        detected = not r.passed
        rep.add(g, "negative health probe",
                Status.PASS if detected else Status.FAIL,
                f"broke [{broke}] -> functionality_test "
                + ("reported FAIL (detects the down service)" if detected else
                   "still PASS — the checker does not detect a down service"))
    except Exception as e:
        rep.add(g, "negative health probe", Status.FAIL,
                f"broke [{broke}] -> functionality_test raised {e!r} instead of "
                f"returning passed=False; the real poller stays GREEN on a raise, "
                f"so it would not flag this down service")

    # Recovery: the same restart the defense workflow runs must heal the break,
    # so the negative probe doesn't leave the service down for good. await_ready
    # tolerates a slow rebind after restart instead of a flaky immediate probe.
    if ch.restart_handler is None:
        return
    try:
        ch.restart_handler.run(target)
        ready, detail = await_ready(ch, target, ready_timeout)
        rep.add(g, "recovery after break",
                Status.PASS if ready else Status.FAIL,
                "healthy again after restart" if ready
                else f"still unhealthy after break+restart: {detail}")
    except Exception as e:
        rep.add(g, "recovery after break", Status.FAIL,
                f"restart/health after break raised {e!r}")
