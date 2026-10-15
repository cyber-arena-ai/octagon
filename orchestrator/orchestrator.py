from __future__ import annotations

import logging
import shlex
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from models.topology import Node, TopologyGraph

from .compose import ComposeManager, classify_nets, render_team_compose, team_node_ids
from .infra.docker import ContainerManager
from .infra.network import NetworkManager
from .naming import qualify

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DevSetupResult:
    """Secret-safe outcome for one dev container setup stage.

    Raw command output is intentionally excluded because package-manager errors
    can contain proxy URLs, headers, or credentials.  Persisted evidence uses a
    closed status vocabulary plus timing and non-secret shape metadata only.
    """

    status: str
    duration_secs: float
    proxy_configured: bool
    rc: int | None = None
    exception_type: str | None = None

    def as_payload(self) -> dict:
        return asdict(self)


class DevSetupFailure(RuntimeError):
    """Carry a classified setup failure without retaining raw output."""

    def __init__(self, result: DevSetupResult) -> None:
        self.result = result
        super().__init__(
            f"dev setup failed (status={result.status}, rc={result.rc})"
        )


def classify_dev_setup_failure(
    output: str,
    *,
    proxy_configured: bool,
    exception_type: str | None = None,
) -> str:
    """Return a closed, secret-safe setup failure class.

    A 403 is proxy-specific only when the dev node actually has a configured
    HTTP(S) proxy.  Timeout, authentication, command, and unexpected exception
    paths remain distinct so readiness evidence stays fail-loud.
    """

    folded = output.casefold()
    exception_folded = (exception_type or "").casefold()
    if "timeout" in exception_folded or "timed out" in folded or "timeout" in folded:
        return "timeout"
    if any(
        marker in folded
        for marker in (
            "401 unauthorized",
            "407 proxy authentication required",
            "authentication failed",
            "authorization failed",
            "invalid api key",
            "invalid token",
        )
    ):
        return "auth_error"
    if proxy_configured and any(
        marker in folded
        for marker in ("403 forbidden", "http 403", "status code 403")
    ):
        return "configured_proxy_http_403"
    if exception_type is not None:
        return "exception"
    return "command_error"


class Orchestrator:
    """Container + network lifecycle for the arena.

    Hybrid bring-up (see docs/UML/orchestrator.md):
      - SHARED networks (span ≥2 teams / infra-only) + shared infra nodes -> SDK
      - each team's PRIVATE net (`<team>_net`) + its dev/prod -> the team's own
        `docker compose` project
      - facility services that declare a team-private net (api-router, mcp-gateway)
        are connected into it AFTER compose creates it (`docker network connect`)

    setup() order:
      1. classify nets into shared vs team-private
      2. SDK-create shared nets only (compose makes the team-private ones)
      3. start infra (SDK) on their shared nets; record their team-private nets
      4. start each team via `docker compose up` (creates `<team>_net`)
      5. connect the deferred facility services into the team nets
      6. run install_list (by name) + pre-flight (stub per design section 2)

    teardown() removes every container by name (SDK) — this reaches the
    compose-created team containers too, so teardown needs no compose knowledge
    and the standalone `main.py teardown` process works without the compose files.
    """

    def __init__(
        self,
        graph: TopologyGraph,
        containers: ContainerManager | None = None,
        network: NetworkManager | None = None,
        compose: ComposeManager | None = None,
        compose_dir: Path | None = None,
        extra_volumes: dict[str, list[str]] | None = None,
        ns: str = "",
    ) -> None:
        self.graph = graph
        self.ns = ns
        self.containers = containers or ContainerManager(ns=ns)
        self.network = network or NetworkManager(graph=graph, ns=ns)
        self.compose = compose or ComposeManager()
        # Where generated per-team compose files are written. main.py points
        # this at runs/<ts>/compose/ for forensics; otherwise a tempdir.
        self.compose_dir = compose_dir
        # Per-node bind-mount overrides: node_id -> ['host:container[:mode]'].
        # main.py sets this to inject a generated apirouter config before setup().
        self.extra_volumes: dict[str, list[str]] = extra_volumes or {}
        # Closed, secret-safe per-dev setup evidence consumed by GameMgr's
        # SETUP_COMPLETE event and the offline dsh20 validator.
        self.dev_setup_results: dict[str, dict] = {}

    def setup(self) -> None:
        log.info("orchestrator setup: %d networks, %d nodes",
                 len(self.graph.networks), len(self.graph.nodes))

        shared, private = classify_nets(self.graph)
        team_ids = team_node_ids(self.graph)

        # 1+2: SDK-create only the shared networks. Team-private nets are created
        # by the owning team's compose project (step 4).
        for net in self.graph.networks:
            if net.name in shared:
                self.network.create(net)

        # 3: shared infra (everything not owned by a team) stays on the SDK so its
        # already-${VAR}-expanded secrets never land in a compose file. Bring each
        # up on its SHARED nets only; defer any team-private net (compose hasn't
        # created it yet) to a connect after compose (step 5).
        deferred: list[tuple[str, str]] = []  # (infra node id, team-private net)
        for node in self.graph.nodes:
            if node.id in team_ids:
                continue
            create_nets = [n for n in node.networks if n in shared]
            self.containers.create_and_start(
                node, networks=create_nets,
                volumes=self.extra_volumes.get(node.id) or None,
            )
            deferred += [(node.id, n) for n in node.networks if n in private]

        # 4: each team -> its own compose project (creates its team-private net and
        # attaches that team's dev+prod).
        self._setup_teams()

        # 5: connect the facility services into the now-compose-created team nets.
        for node_id, net_name in deferred:
            self.containers.connect(net_name, node_id)
            log.info("connected %s into compose-created %s", node_id, net_name)

        # 6: install_list (by name) + dev-deps preflight + pre-flight.
        for node in self.graph.nodes:
            self._run_install_list(node)
        node_ids = {n.id for n in self.graph.nodes}
        for team in self.graph.teams:
            # Only teams _setup_teams actually brought up (dev AND prod present)
            # have a live dev container to install into. Best-effort per team:
            # a dev-setup failure is logged and isolated to that team (it runs
            # degraded), so one bad dev container never aborts the whole run.
            if team.dev in node_ids and team.prod in node_ids:
                try:
                    result = self._run_setup_script(team.dev)
                except DevSetupFailure as exc:
                    result = exc.result
                    log.warning(
                        "dev-setup.sh failed on %s; team runs with a degraded "
                        "toolset (status=%s rc=%s duration=%.3fs)",
                        team.dev,
                        result.status,
                        result.rc,
                        result.duration_secs,
                    )
                self.dev_setup_results[team.dev] = result.as_payload()

        self._preflight()
        log.info("orchestrator setup complete")

    def _setup_teams(self) -> None:
        if not self.graph.teams:
            return
        out_dir = self.compose_dir or Path(tempfile.mkdtemp(prefix="cyberarena-compose-"))
        out_dir.mkdir(parents=True, exist_ok=True)
        node_ids = {n.id for n in self.graph.nodes}
        for team in self.graph.teams:
            if team.dev not in node_ids or team.prod not in node_ids:
                log.warning("team %s has no dev/prod nodes in topology; skip", team.id)
                continue
            compose_file = out_dir / f"{team.id}.yaml"
            compose_file.write_text(render_team_compose(team, self.graph, self.ns))
            log.info("wrote compose file for team %s -> %s", team.id, compose_file)
            # namespace the compose PROJECT too, else concurrent runs share project
            # state (`team1`) and the 2nd `up` reconciles against the 1st's containers.
            self.compose.up(qualify(team.id, self.ns), compose_file)

    def teardown(self) -> None:
        log.info("orchestrator teardown")

        for node in self.graph.nodes:
            self.containers.remove(node.id)

        for net in self.graph.networks:
            self.network.remove(net.name)

        log.info("orchestrator teardown complete")

    def _run_install_list(self, node: Node) -> None:
        if not node.install_list:
            return
        pkgs = " ".join(shlex.quote(p) for p in node.install_list)
        cmd = f"bash -c 'apt-get update && apt-get install -y --no-install-recommends {pkgs}'"
        log.info("install_list on %s: %s", node.id, node.install_list)
        rc, out = self.containers.exec(node.id, cmd)
        if rc != 0:
            raise RuntimeError(f"install_list failed on {node.id} (rc={rc}):\n{out}")

    def _run_setup_script(self, dev_id: str) -> DevSetupResult:
        """Dev-deps preflight (step 6): run dev-setup.sh in the team's dev
        container. Raises on non-zero exit; the caller isolates the failure to
        the affected team (logs + continues) so one bad dev container never
        aborts the whole run.
        """
        script = Path(__file__).parents[1] / "config" / "docker" / "dev-setup.sh"
        node = next((item for item in self.graph.nodes if item.id == dev_id), None)
        proxy_configured = bool(
            node is not None
            and any(
                str((node.env or {}).get(name) or "").strip()
                for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
            )
        )
        log.info("dev-setup.sh on %s", dev_id)
        started = time.monotonic()
        try:
            rc, out = self.containers.exec(
                dev_id, ["bash", "-c", script.read_text()]
            )
        except Exception as exc:
            result = DevSetupResult(
                status=classify_dev_setup_failure(
                    "",
                    proxy_configured=proxy_configured,
                    exception_type=type(exc).__name__,
                ),
                duration_secs=round(max(0.0, time.monotonic() - started), 3),
                proxy_configured=proxy_configured,
                exception_type=type(exc).__name__,
            )
            raise DevSetupFailure(result) from exc
        duration = round(max(0.0, time.monotonic() - started), 3)
        if rc != 0:
            raise DevSetupFailure(
                DevSetupResult(
                    status=classify_dev_setup_failure(
                        out, proxy_configured=proxy_configured
                    ),
                    duration_secs=duration,
                    proxy_configured=proxy_configured,
                    rc=rc,
                )
            )
        log.info("dev-setup.sh OK on %s", dev_id)
        return DevSetupResult(
            status="ok",
            duration_secs=duration,
            proxy_configured=proxy_configured,
            rc=rc,
        )

    def _preflight(self) -> None:
        # MVP stub per design section 2.
        log.info("pre-flight: starting checks")
        log.info("pre-flight: all checks passed")
