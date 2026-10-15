# Challenge interface v2 — pluggable Challenge ABC

> **Status: shipped 2026-06-03.** The v1 `ChallengeSource` /
> `ChallengeSpec` / `FlagMechanism` / `Service` surface was deleted in
> the same change. The live architecture lives in
> [docs/UML/challenges.md](../UML/challenges.md); contributor-facing
> instructions live in [docs/CHALLENGE_AUTHORING_SOP.md](../CHALLENGE_AUTHORING_SOP.md).
> This document is kept as a short record of what changed and why.

## Why v2

Under v1, adding a CTF challenge touched five different framework
files (`challenges/source.py` for metadata, `FlagServer` for plant
dispatch, `HealthChecker` for the hardcoded 4-level chain,
`agents/prompts/*` for prompts, `orchestrator/service.py` for the
SERVICE_PROTOCOL adapter). The `FlagMechanism` enum
(SERVICE_PROTOCOL / FILE_AT_PATH / ENV_VAR / BINARY_PATCH) had
speculative entries with no implementation.

v2 collapses everything for one challenge into one directory behind a
single ABC root. Framework code no longer dispatches by mechanism — it
just calls methods on the `Challenge` instance.

## What v2 is

`Challenge` is a composition root with five components:

| Component | Responsibility |
|---|---|
| `VulboxImage` | image reference + exposed ports + build context |
| `FunctionalityTest.run(target)` | returns a `CheckResult` tree. Only requirement: one leaf named `checker` (or any leaf that fails when the service's real function breaks). Transport-level leaves alongside it are recommended but free-form. |
| `FlagHandler.plant(target, flag)` / `retrieve(target, handle)` | opaque per-challenge plant + retrieve |
| `RestartHandler.run(target)` | rebuild + respawn after agent defense edits |
| `prompts.build_attack_prompt` / `build_defense_prompt` | per-challenge prompts |

Framework-side, `GameMgr.target_for(team_id, challenge)` is the single
helper that constructs a `VulboxTarget` (host + named-port map + a
`meta["exec_in_container"]` callable). Handlers reach into `meta` for
docker exec today; swapping in k8s / real SSH would change only the
callable, not the handler code.

## Decisions pinned

- **`RestartHandler` is a probe subclass**, not a metadata string. Attack-only challenges leave `Challenge.restart_handler = None`; the orchestrator skips DEFENSE_KICKOFF for them. (The authoring SOP forbids this for contributed challenges — defense is mandatory.)
- **`HealthLevel` enum survives as recommended naming** for `CheckResult` children. The framework no longer branches on its values; the panel still renders `level` as a tag.
- **`FlagMechanism` enum retired.** Each `FlagHandler.plant` is opaque to the framework.
- **Prompts dispatch stays in `agents/prompts/__init__.py`** (one lookup by `Challenge.name`). The per-challenge `prompts.py` defines the actual prompt bodies.
- **`VulboxTarget.meta` is a deliberately small key set.** Documented in `challenges/interface/probe.py`. New handlers should reuse `exec_in_container`, not add their own escape hatches.

## After v2

Adding a new challenge is one PR touching only `challenges/<snake>/`
plus 2-4 lines in [`challenges/registry.py`](../../challenges/registry.py).
Zero touches to `gamemgr/`, `flag/`, `health/`, or
`agents/prompts/__init__.py`. That's the test of whether v2 worked.

See [CHALLENGE_AUTHORING_SOP](../CHALLENGE_AUTHORING_SOP.md) for the
contributor-facing instructions.
