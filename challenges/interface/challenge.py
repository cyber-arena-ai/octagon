"""`Challenge` ABC — the unit one CTF problem packages itself as."""

from __future__ import annotations

import inspect
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, ClassVar

from .probe import (
    DEFAULT_STORE,
    FlagHandler,
    FlagStoreSpec,
    FunctionalityTest,
    OracleKind,  # noqa: F401 — re-exported for authors writing flag_store_specs()
    RestartHandler,
    VulboxTarget,
)

log = logging.getLogger(__name__)

ExecFn = Callable[[str, str], tuple[int, str]]
"""(container_name, cmd) -> (rc, stdout). Privileged docker exec."""

# How long `initial_start` polls the primary service port before giving up and
# proceeding anyway. A healthy service early-exits the poll the instant its port
# binds, so only a never-binding service pays the full wait. 120s matches the
# value the heavy DB-/JVM-backed challenges (eight-bit-oracle, certified-
# transparency, …) independently chose for their own hand-rolled port-waits —
# 30s was too short for a CMS+DB or a big Node app to start serving.
_READY_PORT_TIMEOUT_SECS = 120

# Authoritative-state reads are deliberately small snapshots, not a general
# container export API. Four MiB is enough for a focused config/object region
# while bounding host and flag-service memory for every read.
MAX_STATE_REGION_BYTES = 4 * 1024 * 1024
_STATE_REGION_RE = re.compile(r"[a-zA-Z0-9_.-]+")


def _await_service_port(name: str, target: VulboxTarget, exec_in: ExecFn) -> None:
    """Block until the primary service port accepts a TCP connection inside the
    container, or `_READY_PORT_TIMEOUT_SECS` elapses. Soft: never raises — a
    missing port, an unrunnable probe, or a timeout only logs a warning."""
    if not target.ports:
        return
    port = target.ports.get("service") or next(iter(target.ports.values()))
    # bash builtin /dev/tcp — `exec_in` already runs `bash -c`.
    probe = (
        f"for i in $(seq 1 {_READY_PORT_TIMEOUT_SECS}); do "
        f"(exec 3<>/dev/tcp/127.0.0.1/{port}) 2>/dev/null && exit 0; "
        f"sleep 1; done; exit 1"
    )
    try:
        rc, _out = exec_in(target.host, probe)
    except Exception as e:  # noqa: BLE001 — readiness is best-effort, never fatal
        log.warning("%s initial_start: readiness probe could not run (%s); "
                    "proceeding without waiting", name, e)
        return
    if rc != 0:
        log.warning("%s initial_start: service port %s not listening after %ds; "
                    "first flag plant may race startup", name, port,
                    _READY_PORT_TIMEOUT_SECS)


@dataclass(frozen=True)
class VulboxImage:
    """Docker image that runs the vulnerable app.

    Fields:
      reference     — image tag (registry pull or local build)
      exposed_ports — name -> container port; names must match the paired
                      probe's `required_ports`
      env           — default env vars baked into deployment
      build_context — path to a Dockerfile dir; if set, the orchestrator
                      builds before deploying. None = pull from registry.
    """

    reference: str
    exposed_ports: dict[str, int]
    env: dict[str, str] = field(default_factory=dict)
    build_context: str | None = None


@dataclass(frozen=True)
class ChallengeDocs:
    """Authoring-time docs bundled with a challenge.

      intro          — human-facing description (dashboard / writeups)
      agent_prompts  — role-keyed prompts surfaced to autonomous agents
                       (unused in the current dispatcher — kept for plugins)
      func_test_spec — human-readable spec for what FunctionalityTest checks
    """

    intro: str
    agent_prompts: dict[str, str]
    func_test_spec: str


@dataclass(frozen=True)
class StateRegionSpec:
    """One application-state region the trusted facility may read.

    ``name`` is the only identifier challenge handlers use at runtime. ``path``
    stays host-side and is never accepted from a player claim. It names a file
    or directory in the victim prod container whose bytes are authoritative for
    the application. Reads are performed by the host Docker API, never by code
    executed in the defender-controlled container.
    """

    name: str
    path: str
    max_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _STATE_REGION_RE.fullmatch(self.name):
            raise ValueError(
                "state region name must match "
                f"{_STATE_REGION_RE.pattern!r}: {self.name!r}"
            )
        if not isinstance(self.path, str) or "\x00" in self.path:
            raise ValueError("state region path must be a nonempty absolute path")
        path = PurePosixPath(self.path)
        if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
            raise ValueError(
                "state region path must be absolute, non-root, and contain no '..': "
                f"{self.path!r}"
            )
        if (
            not isinstance(self.max_bytes, int)
            or isinstance(self.max_bytes, bool)
            or not 0 < self.max_bytes <= MAX_STATE_REGION_BYTES
        ):
            raise ValueError(
                f"state region max_bytes must be 1..{MAX_STATE_REGION_BYTES}"
            )


def validate_state_region_specs(
    specs: list[StateRegionSpec], *, challenge: str = "",
) -> None:
    """Fail fast on duplicate or non-spec authoritative-state declarations."""
    where = f" for challenge {challenge!r}" if challenge else ""
    if any(not isinstance(spec, StateRegionSpec) for spec in specs):
        raise ValueError(
            f"authoritative_state_regions(){where} must return "
            "StateRegionSpec values"
        )
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ValueError(
            f"authoritative_state_regions(){where} has duplicate names: {names}"
        )


class Challenge(ABC):
    """One CTF challenge, packaged for the orchestrator.

    The same instance is reused across teams — per-team state goes on the
    `VulboxTarget` passed to probes at call time, never on `self`.
    """

    name: ClassVar[str]
    """Stable kebab-case id (`"notes"`, `"nautilus-ico"`).

    A class-level attribute, NOT a property — the registry reads it off the
    class during discovery without instantiating (instantiation is allowed to
    do heavy work like `git rev-parse`). Every concrete challenge must set it;
    discovery validates it is a non-empty kebab-case string.
    """

    @property
    @abstractmethod
    def vulbox(self) -> VulboxImage: ...

    @property
    @abstractmethod
    def functionality_test(self) -> FunctionalityTest: ...

    @property
    @abstractmethod
    def flag_handler(self) -> FlagHandler: ...

    def flag_stores(self) -> list[str]:
        """Named flag stores this challenge hosts. One flag is planted per store
        each round, and each is scored independently.

        Default is a single `DEFAULT_STORE` — the single-flag case (what
        `SingleFlagHandler` handles). A multiflag challenge overrides this with its
        named stores (e.g. `["document", "profile"]`) and implements
        `FlagHandler.plant` to write each. Names must be unique and match
        `[a-zA-Z0-9_.-]+`; the framework validates this at bring-up.
        """
        return [DEFAULT_STORE]

    def flag_store_specs(self) -> list[FlagStoreSpec]:
        """The stores plus WHICH capability each measures and how it scores —
        **the single authoritative declaration**. Everything downstream (planting,
        scoring, flag counts, the verifier's checkers) reads this and nothing else.

        `flag_stores()` is kept as the simple authoring hook for the common
        all-READ case, and the default here wraps it — so existing read
        challenges need no change. A challenge exposing a write or execution
        outcome overrides **this method ONLY**; do not also override
        `flag_stores()`, which would just restate the names in a view nothing
        reads:

            def flag_store_specs(self):
                return [
                    FlagStoreSpec("report", scope="app.cross_boundary.read"),
                    FlagStoreSpec("audit_log", OracleKind.WRITE,
                                  scope="app.cross_boundary.write"),
                ]
        """
        return [FlagStoreSpec(name) for name in self.flag_stores()]

    def authoritative_state_regions(self) -> list[StateRegionSpec]:
        """Static allowlist for facility-side authoritative-state reads.

        READ and EXECUTE stores need none. A WRITE challenge must declare at
        least one region: only the smallest file or directory its claim-side
        `retrieve()` must inspect, read through `read_authoritative_state` by
        opaque ``name``. It never supplies a path or command. This keeps claim
        evidence outside the defender-controlled in-container reader.
        """
        return []

    @property
    def restart_handler(self) -> RestartHandler | None:
        """Rebuild + restart hook for the defense workflow.

        Default `None` is reserved for framework-internal test
        challenges. Externally contributed challenges must override.
        """
        return None

    @property
    @abstractmethod
    def docs(self) -> ChallengeDocs: ...

    def initial_start(self, target: VulboxTarget, exec_in: ExecFn) -> None:
        """Once-per-game first-start hook, run after the container is up.

        For image-baked challenges whose entrypoint spawns the service
        itself, the default waits for the primary service port to start
        accepting connections (inside the container) before returning, so
        the framework doesn't plant the first flag the instant after deploy
        and lose a race against a still-booting web service (the failure is
        a connection-refused on the plant, which aborts the whole match).

        Soft by design: a missing port, a probe that can't run, or a service
        that never binds within the window only logs — it never raises. A
        genuinely dead service still surfaces downstream at flag-plant /
        health, where the diagnostic is clearer, not here.

        Challenges that bake the binary but don't spawn it on boot inherit
        `RestartScriptInitialStart` instead.
        """
        _await_service_port(self.name, target, exec_in)

    def break_service(self, target: VulboxTarget, exec_in: ExecFn) -> str | None:
        """Optional negative-test hook: reliably take the live service DOWN
        (e.g. stop the app process) WITHOUT removing the container, then return a
        short description of what was done. The break must be something
        `restart_handler` undoes. Default `None` = no hook, so the readiness gate
        skips its down-service-detection check.
        """
        return None

    @property
    def health_interval_secs(self) -> float | None:
        """Intrinsic health-poll interval for this challenge, or `None` to
        defer to the framework default. A `game.yaml` entry may override it
        per match. Tie the value to checker cost (e.g. a checker that hits
        the live service each tick wants a slower poll). Must be positive."""
        return None

    # ── facility-side dependency setup (v3) ─────────────────────────────
    # The flag-service / health-poller run a challenge's flag_handler /
    # functionality_test IN THEIR OWN containers, so they need that probe's
    # Python deps. A challenge declares them by dropping a shell script in its
    # package, run at facility-image BUILD time (per game, only for deployed
    # challenges). Split per facility because the probes load independently
    # (see lazy probe properties): the flag-service builds with the FLAG script,
    # the poller with the HEALTH script. Most challenges need neither (their
    # probes are stdlib / exec one-liners inside prod).

    @property
    def flag_facility_setup_script(self) -> Path | None:
        """`challenges/<name>/flag_facility_setup.sh` if present — installs the
        flag_handler's facility-side deps into the flag-service image."""
        return self._facility_setup_script("flag_facility_setup.sh")

    @property
    def health_facility_setup_script(self) -> Path | None:
        """`challenges/<name>/health_facility_setup.sh` if present — installs the
        functionality_test's facility-side deps into the health-poller image."""
        return self._facility_setup_script("health_facility_setup.sh")

    def _facility_setup_script(self, filename: str) -> Path | None:
        p = Path(inspect.getfile(type(self))).parent / filename
        return p if p.is_file() else None


class RestartScriptInitialStart:
    """`initial_start` that kicks a restart entrypoint (spawn == restart).

    Mix into challenges that bake the service but don't spawn it on container
    boot: the same script both first-spawns and rebuilds-after-defense, so one
    code path serves both. Override `restart_script` if the path differs.
    """

    restart_script: ClassVar[str] = "/arena/restart.sh"

    def initial_start(self, target: VulboxTarget, exec_in: ExecFn) -> None:
        rc, out = exec_in(target.host, self.restart_script)
        if rc != 0:
            log.warning(
                "%s initial_start: %s rc=%d out=%r",
                self.name, self.restart_script, rc, out[-200:] if out else "",
            )
        # The restart script returns once it has *spawned* the service, which
        # may bind its port a few seconds later. Wait for it, same as the
        # default path, so the framework doesn't plant the first flag (or let
        # an attacker connect) against a still-booting service.
        _await_service_port(self.name, target, exec_in)
