"""Trusted authoritative-state transport: host archive read + HTTP boundary."""

from __future__ import annotations

import io
import tarfile
from unittest.mock import MagicMock

import pytest

from orchestrator.infra.docker import ContainerManager


def _archive(entries: list[tuple[str, bytes | None, str]]) -> bytes:
    """Build a tar: kind is file, dir, or symlink."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name=name)
            if kind == "dir":
                info.type = tarfile.DIRTYPE
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/shadow"
            else:
                assert content is not None
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
                continue
            tar.addfile(info)
    return out.getvalue()


def _manager(archive: bytes, *, ns: str = ""):
    container = MagicMock()
    container.get_archive.return_value = (iter([archive]), {"size": len(archive)})
    client = MagicMock()
    client.containers.get.return_value = container
    return ContainerManager(client=client, ns=ns), client, container


def test_read_files_uses_host_archive_without_container_exec():
    archive = _archive([
        ("private", None, "dir"),
        ("private/config.xml", b"<description>MARK</description>", "file"),
        ("private/blob", b"\x00\xff", "file"),
    ])
    manager, client, container = _manager(archive, ns="run1")

    files = manager.read_files("team1_prod", "/srv/private", max_bytes=1024)

    assert files == {
        "private/config.xml": b"<description>MARK</description>",
        "private/blob": b"\x00\xff",
    }
    client.containers.get.assert_called_once_with("run1-team1_prod")
    container.get_archive.assert_called_once_with("/srv/private")
    container.exec_run.assert_not_called()


def test_read_files_rejects_links_and_oversized_content():
    manager, _, _ = _manager(_archive([("region/link", None, "symlink")]))
    with pytest.raises(ValueError, match="non-file"):
        manager.read_files("prod", "/region", max_bytes=1024)

    manager, _, _ = _manager(_archive([("region/big", b"012345", "file")]))
    with pytest.raises(ValueError, match="size limit"):
        manager.read_files("prod", "/region", max_bytes=5)


def test_read_files_accepts_a_readable_empty_directory():
    manager, _, _ = _manager(_archive([("region", None, "dir")]))
    assert manager.read_files("prod", "/region", max_bytes=1024) == {}
