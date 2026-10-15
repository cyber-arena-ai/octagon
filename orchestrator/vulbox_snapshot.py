"""End-of-run snapshot of the patched vulbox(es) — the offline-verification handoff.

Opt-in via `GameConfig.snapshot_patched_vulbox` (default off). After the game
ends but BEFORE teardown removes the containers, `docker commit` each team's prod
container (the service the agent hardened) and `docker save` the resulting image
into the run dir, plus a `manifest.json`. A later reviewer loads the tar
(`docker load -i <tar>`) and does offline verification — run the ground-truth
PoC against the patched box, diff the source, etc.

Output layout under the run dir:
    patched-vulbox/
      <team>-<challenge>.tar     # `docker save --named` (keeps the repo:tag)
      manifest.json              # one entry per snapshot (base image, tag, size)

Best-effort per container: a failure on one box is logged and skipped, never
fatal to teardown. Off by default because each tar is large (a full challenge
image). Mode-agnostic, but intended for solo runs.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .naming import qualify

log = logging.getLogger(__name__)

_SUBDIR = "patched-vulbox"


def _safe(component: str) -> str:
    """A docker repo/tag component: lowercase, keep [a-z0-9._-], collapse the
    rest to '-'. Docker rejects other characters (e.g. the ':' / uppercase a
    run-dir timestamp or team id might carry)."""
    return re.sub(r"[^a-z0-9._-]+", "-", component.lower()).strip("-._") or "run"


def snapshot_patched_vulboxes(client, *, run_dir, teams, challenges, ns=""):
    """Commit + save each team's prod container; write a manifest. Returns the
    list of manifest entries (empty if nothing was snapshotted).

    Args:
      client     — a docker client (`orch.containers.client`).
      run_dir    — the run's output dir (Path/str); output lands under `patched-vulbox/`.
      teams      — topology teams (each `.prod` is the container to snapshot).
      challenges — challenge configs, used to name the image by the deployed challenge.
      ns         — orchestrator namespace, to qualify the container name.
    """
    out_dir = Path(run_dir) / _SUBDIR
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = _safe(Path(run_dir).name)

    # team_id -> the (first) challenge deployed on it, to name the image.
    chal_for: dict[str, str] = {}
    for c in challenges:
        for team_id in c.deploy_on:
            chal_for.setdefault(team_id, c.name)

    manifest: list[dict] = []
    for team in teams:
        if not team.prod:
            continue
        cname = qualify(team.prod, ns)
        challenge = chal_for.get(team.id, team.prod)
        repo = f"cyberarena/patched-{_safe(challenge)}"
        tag = f"{run_id}-{_safe(team.id)}"

        # Commit first; if that fails there's no image to clean up.
        try:
            container = client.containers.get(cname)
            base_image = (container.image.tags or [container.image.id])[0]
            image = container.commit(repository=repo, tag=tag)
        except Exception:
            log.exception("snapshot: commit failed for %s; skipping", cname)
            continue

        # The committed image now exists in the daemon. The tar (docker save
        # --named) is the only deliverable, so the image is temporary — remove it
        # in a `finally` so it can't leak even if save/write fails partway, and
        # so a removal failure only WARNS (never overrides the snapshot result).
        # `tar_path` is computed OUTSIDE the try so it is always bound for the
        # partial-tar cleanup in the except.
        tar_name = f"{_safe(team.id)}-{_safe(challenge)}.tar"
        tar_path = out_dir / tar_name
        try:
            with open(tar_path, "wb") as fh:
                for chunk in image.save(named=True):
                    fh.write(chunk)
            entry = {
                "team": team.id,
                "challenge": challenge,
                "container": cname,
                "base_image": base_image,
                "committed_image": f"{repo}:{tag}",
                "tar": f"{_SUBDIR}/{tar_name}",
                "size_bytes": tar_path.stat().st_size,
            }
            manifest.append(entry)
            log.info(
                "snapshot: committed %s -> %s, saved %s (%d bytes)",
                cname, entry["committed_image"], entry["tar"], entry["size_bytes"],
            )
        except Exception:
            log.exception("snapshot: save failed for %s; skipping", cname)
            # Don't leave a truncated tar behind (no manifest entry refers to it).
            # Best-effort: only log if the unlink itself fails.
            try:
                tar_path.unlink(missing_ok=True)
            except Exception:
                log.warning(
                    "snapshot: could not remove partial tar %s; leaving it",
                    tar_path, exc_info=True,
                )
        finally:
            try:
                client.images.remove(image.id, force=True)
            except Exception:
                log.warning(
                    "snapshot: could not remove temporary image %s (%s); leaving it",
                    getattr(image, "id", "?"), f"{repo}:{tag}", exc_info=True,
                )

    (out_dir / "manifest.json").write_text(
        json.dumps({"snapshots": manifest}, indent=2)
    )
    log.info("snapshot: wrote %d patched-vulbox image(s) to %s", len(manifest), out_dir)
    return manifest
