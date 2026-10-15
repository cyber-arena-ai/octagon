"""Per-game facility image builder (v3).

Build `cyberarena/flag-service` + `cyberarena/health-poller` for THIS game by
layering the deployed challenges onto the lean base images
(`cyberarena/flag-service-base` / `cyberarena/health-poller-base`):

  flag-service  ← base + each deployed challenge's package + flag_facility_setup.sh
  health-poller ← base + each deployed challenge's package + health_facility_setup.sh

Two things are layered per deployed challenge. (1) The challenge PACKAGE itself
(challenges.<name>): the probes — flag_handler / functionality_test — run inside
these containers, but since the warehouse split the challenge code lives in the
selected challenge warehouse and is NOT in the lean base, so it's copied in here
(vendor/ excluded — it's prod-image build source, never imported by the probes).
(2) The challenge's facility setup script, if any.

Each script installs that challenge's probe deps. Only deployed challenges, and
only the relevant facility (the flag-service never gets the poller's deps and
vice versa — the probes import independently). No per-challenge images; one lean
base, a thin per-game layer. A game whose challenges are all stdlib-side just
re-tags the base.
"""

from __future__ import annotations

import inspect
import logging
import shutil
import tempfile
from pathlib import Path

import docker

from challenges.registry import build_challenge

log = logging.getLogger(__name__)

# (image basename, the Challenge property naming that facility's setup script)
_FACILITIES = [
    ("flag-service", "flag_facility_setup_script"),
    ("health-poller", "health_facility_setup_script"),
]

# Excluded when copying a challenge package into a facility image: vendor/ is
# build-time source for the PROD challenge image (cloned by sync scripts, can be
# 100s of MB) — the facility probes (flag_handler / functionality_test) never
# import it, they plant/check over SSH. .git/__pycache__ are noise.
_PKG_IGNORE = shutil.ignore_patterns("vendor", ".git", "__pycache__", "*.pyc")


def _package_dir(challenge) -> Path:
    """Filesystem directory of a challenge's Python package (the dir holding its
    __init__.py), resolved from the Challenge class's source file. Works for any
    contributor layout under the warehouse — the importable name is the dir name."""
    return Path(inspect.getfile(type(challenge))).resolve().parent


def build_facility_images(cfg, ns: str = "", *, client=None) -> list[str]:
    """Build the per-slot flag-service + health-poller images. Returns the tags
    built. Idempotent-ish: re-runs the docker build (layer cache makes it cheap
    when scripts are unchanged).

    `ns` is the run namespace (the worker's slot id, e.g. "s2"; empty on a
    single-slot runner) and it goes in the TAG. It has to: the image carries the
    deployed challenges' packages, so two games on one host that build the same
    tag overwrite each other, and whichever starts its container second gets an
    image built for the OTHER game's challenge. Its probe import then dies with
    `ModuleNotFoundError: challenges.<its own challenge>` and /flags/configure
    500s at setup. Containers were already namespaced (`s2-flag_service`); the
    tag was not.

    The namespace is per SLOT and reused across games, so this does not grow
    images without bound — a 4-slot host holds 8 facility images, each rebuilt
    in place. An empty ns means a single-slot runner, which cannot race itself,
    so `latest` is safe there.
    """
    client = client or docker.from_env()
    names = sorted({c.name for c in cfg.challenges})
    challenges = {n: build_challenge(n) for n in names}

    # Deployed challenge packages, deduped by dir: the probes run INSIDE the
    # facility containers, so each container needs the challenge code. The lean
    # base only carries the framework (challenges.interface + registry); the
    # warehouse packages live outside the framework package and are NOT
    # in the base image, so copy each deployed one in here as challenges.<name>.
    pkg_dirs = sorted({_package_dir(challenges[n]) for n in names})

    built: list[str] = []
    for facility, script_attr in _FACILITIES:
        base = f"cyberarena/{facility}-base"
        tag = f"cyberarena/{facility}:{ns or 'latest'}"
        scripts = [
            (n, Path(p)) for n in names
            if (p := getattr(challenges[n], script_attr)) is not None
        ]
        with tempfile.TemporaryDirectory(prefix="cyberarena-facbuild-") as tmp:
            ctx = Path(tmp)
            lines = [f"FROM {base}"]
            for pkg_dir in pkg_dirs:
                shutil.copytree(pkg_dir, ctx / pkg_dir.name, ignore=_PKG_IGNORE)
                lines.append(f"COPY {pkg_dir.name}/ /app/challenges/{pkg_dir.name}/")
            for n, src in scripts:
                shutil.copyfile(src, ctx / f"{n}.sh")
                lines += [f"COPY {n}.sh /tmp/facility-setup/{n}.sh",
                          f"RUN bash /tmp/facility-setup/{n}.sh"]
            (ctx / "Dockerfile").write_text("\n".join(lines) + "\n")
            log.info("building %s from %s (+%d challenge setup script(s): %s)",
                     tag, base, len(scripts), [n for n, _ in scripts])
            # Arch comes from the base (its Dockerfile pins linux/amd64); don't
            # pass platform= here — with the local-only base it makes the SDK try
            # to PULL the base. pull=False keeps the local base.
            client.images.build(path=str(ctx), tag=tag, rm=True, pull=False)
        built.append(tag)
    return built
