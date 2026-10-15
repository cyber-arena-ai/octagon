from __future__ import annotations

import io
import logging
import tarfile
from pathlib import PurePosixPath

import docker
from docker.errors import APIError, NotFound

from models.topology import Node
from orchestrator.naming import qualify

log = logging.getLogger(__name__)

_MAX_STATE_FILES = 256
_MAX_TAR_OVERHEAD_BYTES = 1024 * 1024


class ContainerManager:
    """Manages docker containers for the arena."""

    def __init__(self, client: docker.DockerClient | None = None, ns: str = "") -> None:
        self._client = client or docker.from_env()
        self.ns = ns

    @property
    def client(self) -> docker.DockerClient:
        """Public read-only accessor for the docker client, for callers that need
        the raw SDK (e.g. commit/save a container). The attribute stays private."""
        return self._client

    def _q(self, name: str) -> str:
        return qualify(name, self.ns)

    def create_and_start(
        self,
        node: Node,
        networks: list[str] | None = None,
        volumes: list[str] | None = None,
    ) -> str:
        """Create + start a container per Node spec; attach the given networks.

        `networks` defaults to `node.networks`; pass an explicit subset to bring a
        node up on only some of its declared nets (e.g. its shared nets, deferring
        compose-created team nets until those exist — see Orchestrator.setup).

        `volumes` is an optional list of 'host:container[:mode]' bind-mount specs.

        Idempotent on name: removes any pre-existing container with the
        same name first.
        """
        self.remove(node.id)

        nets = node.networks if networks is None else networks
        ports = self._parse_ports(node.ports)
        primary_net = nets[0] if nets else None

        # NS≠"": the docker container name is namespaced, but `node.id` is kept as a
        # network alias so in-network DNS (ssh team1_prod, http://api-router:8080)
        # is unchanged. NS="": no networking_config, byte-identical to before.
        net_config = None
        if self.ns and primary_net:
            ep = self._client.api.create_endpoint_config(aliases=[node.id])
            net_config = self._client.api.create_networking_config(
                {self._q(primary_net): ep})

        container = self._client.containers.create(
            image=node.image,
            name=self._q(node.id),
            hostname=node.id,
            command=None,
            network=self._q(primary_net) if primary_net else None,
            networking_config=net_config,
            ports=ports,
            environment=node.env,
            extra_hosts=node.extra_hosts or None,
            volumes=self._parse_volumes(volumes) if volumes else None,
            detach=True,
        )
        log.info("created container %s (%s, image=%s)", self._q(node.id), container.short_id, node.image)

        for extra_net in nets[1:]:
            self.connect(extra_net, node.id)

        container.start()
        log.info("started container %s", self._q(node.id))
        return container.id

    def connect(self, net_name: str, container_name: str) -> None:
        """Attach an existing container to an existing network (by logical name).
        The container is given `container_name` as a network alias so peers resolve
        it by its bare node id regardless of the namespaced docker name."""
        container = self._client.containers.get(self._q(container_name))
        aliases = [container_name] if self.ns else None
        self._client.networks.get(self._q(net_name)).connect(container, aliases=aliases)
        log.info("connected %s to %s", self._q(container_name), self._q(net_name))

    def remove(self, name: str) -> None:
        """Stop + remove a container. Idempotent. `name` is the logical node id."""
        try:
            c = self._client.containers.get(self._q(name))
        except NotFound:
            return
        try:
            c.stop(timeout=3)
        except APIError:
            pass
        c.remove(force=True)
        log.info("removed container %s", self._q(name))

    def exec(self, name: str, cmd: list[str] | str) -> tuple[int, str]:
        """Run a command inside a container; return (exit_code, output)."""
        c = self._client.containers.get(self._q(name))
        result = c.exec_run(cmd, demux=False)
        out = result.output.decode(errors="replace") if result.output else ""
        return result.exit_code, out

    def read_files(self, name: str, path: str, *, max_bytes: int) -> dict[str, bytes]:
        """Read a bounded file/directory snapshot without executing in prod.

        Docker's archive endpoint is served by the host daemon. The defender can
        change the application state being measured, but cannot replace a shell,
        reader binary, or stdout producer because none runs in the container.
        """
        container = self._client.containers.get(self._q(name))
        stream, _stat = container.get_archive(path)

        archive = bytearray()
        archive_limit = max_bytes + _MAX_TAR_OVERHEAD_BYTES
        for chunk in stream:
            archive.extend(chunk)
            if len(archive) > archive_limit:
                raise ValueError("authoritative-state archive exceeds size limit")

        files: dict[str, bytes] = {}
        total = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                for member in tar:
                    if member.isdir():
                        continue
                    # Links and device/special files can redirect or block a read;
                    # an authoritative state region must resolve to regular bytes.
                    if not member.isfile():
                        raise ValueError(
                            f"authoritative-state region contains non-file {member.name!r}"
                        )
                    relative = PurePosixPath(member.name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ValueError("authoritative-state archive has unsafe member path")
                    name_in_snapshot = relative.as_posix()
                    if name_in_snapshot in files:
                        raise ValueError("authoritative-state archive has duplicate members")
                    if len(files) >= _MAX_STATE_FILES:
                        raise ValueError("authoritative-state region has too many files")
                    if member.size < 0 or total + member.size > max_bytes:
                        raise ValueError("authoritative-state contents exceed size limit")
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        raise ValueError("authoritative-state file could not be read")
                    content = extracted.read(member.size + 1)
                    if len(content) != member.size:
                        raise ValueError("authoritative-state file size changed during read")
                    files[name_in_snapshot] = content
                    total += len(content)
        except tarfile.TarError as exc:
            raise ValueError("invalid authoritative-state archive") from exc
        return files

    def is_running(self, name: str) -> bool:
        try:
            c = self._client.containers.get(self._q(name))
        except NotFound:
            return False
        c.reload()
        return c.status == "running"

    @staticmethod
    def _parse_volumes(specs: list[str]) -> dict[str, dict]:
        """Parse docker volume spec: 'host:container[:mode]'."""
        out: dict[str, dict] = {}
        for spec in specs:
            parts = spec.split(":")
            if len(parts) == 2:
                host, container = parts
                out[host] = {"bind": container, "mode": "rw"}
            elif len(parts) == 3:
                host, container, mode = parts
                out[host] = {"bind": container, "mode": mode}
            else:
                raise ValueError(f"bad volume spec: {spec!r}")
        return out

    @staticmethod
    def _parse_ports(specs: list[str]) -> dict[str, int | tuple[str, int]]:
        """Parse docker port spec.

        '8001:80'             -> {'80/tcp': 8001}            (bind 0.0.0.0:8001)
        '127.0.0.1:8001:80'   -> {'80/tcp': ('127.0.0.1', 8001)}  (bind loopback only)
        """
        out: dict[str, int | tuple[str, int]] = {}
        for spec in specs:
            parts = spec.split(":")
            if len(parts) == 2:
                host, container = parts
                out[f"{int(container)}/tcp"] = int(host)
            elif len(parts) == 3:
                ip, host, container = parts
                out[f"{int(container)}/tcp"] = (ip, int(host))
            else:
                raise ValueError(f"bad port spec: {spec!r}")
        return out