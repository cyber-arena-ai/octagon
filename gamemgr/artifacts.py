"""Per-run artifact persistence — typed path allocator.

Forensic / replay / scoring milestones drop their outputs into stable
paths under `runs/<utc-ts>/artifacts/<team>/round-<N>/<filename>`.

Layout groups by team + round first (matches how operators read a run:
"show me everything team1 did in round 2"); the artifact KIND lives in
the filename via stable conventions (`stream-defense.jsonl` /
`agent-attack.log` / `docker.log` / ...).

The `ArtifactKind` enum is kept as a closed-set type-tag so callers
don't invent new categories ad-hoc, but it no longer influences the
path — only the filename does. Each kind has a canonical filename
convention documented inline.

See docs/archived/DEFCON_challenge_framework_plan_v1.md for the
historical design that introduced this layout.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path


class ArtifactKind(str, Enum):
    """Closed-set tag for artifacts. Doesn't affect the on-disk path —
    use the conventional filenames below so consumers can grep one kind.

    Filename conventions per kind (see callers in `gamemgr.game_mgr`):

      API_LOG          stream-{phase}.jsonl   (LLM stream-json transcript)
      AGENT_LOG        agent-{phase}.log      (raw combined stdout+stderr)
      SERVICE_LOG      docker.log             (docker logs of team prod)
      SOURCE_SNAPSHOT  source.tar             (reserved)
      BINARY_DIFF      binary.diff            (reserved)
      PCAP             traffic.pcap           (reserved)
      EXPLOIT          exploit.bin            (reserved)
      REPLAY_BUNDLE    replay.tar             (reserved)
    """

    API_LOG = "api-log"
    AGENT_LOG = "agent-log"
    SERVICE_LOG = "service-log"
    SOURCE_SNAPSHOT = "source-snapshot"
    BINARY_DIFF = "binary-diff"
    PCAP = "pcap"
    EXPLOIT = "exploit"
    REPLAY_BUNDLE = "replay-bundle"


_ARTIFACTS_DIRNAME = "artifacts"


_README = """\
# Run artifacts

Per-run forensic outputs. Layout:

```
artifacts/<team>/round-<N>/<filename>
```

Per-kind filename conventions (see `gamemgr.artifacts.ArtifactKind`):

- `stream-defense.jsonl` / `stream-attack.jsonl` — LLM stream-json
  transcript (api-log) for that phase: every turn's prompt, assistant
  message, tool_use, tool_result, usage tokens.
- `agent-defense.log` / `agent-attack.log` — raw combined CLI output
  (agent-log) for that phase: full stdout (the stream-json) followed by
  stderr. The unparsed original archive; intentionally overlaps
  `stream-*.jsonl`. stderr is appended last (separate-pipe capture), so
  it is concatenated, not time-interleaved.
- `docker.log` — `docker logs --timestamps <team{N}_prod>` snapshot at
  end of round (service-log).
- `source.tar`, `binary.diff`, `traffic.pcap`, `exploit.bin`,
  `replay.tar` — reserved for future milestones.
"""


class ArtifactStore:
    """Allocator for artifact file paths within one run directory."""

    def __init__(self, run_dir: Path | str) -> None:
        self._run_dir = Path(run_dir)
        self._root = self._run_dir / _ARTIFACTS_DIRNAME
        self._root.mkdir(parents=True, exist_ok=True)
        readme = self._root / "README.md"
        if not readme.exists():
            readme.write_text(_README)

    def path_for(
        self,
        kind: ArtifactKind,
        *,
        team_id: str,
        round_id: int,
        filename: str,
    ) -> Path:
        """Return runs/<ts>/artifacts/<team_id>/round-<N>/<filename>.

        `kind` is kept as a closed-set tag (callers can't invent new
        categories) but does not affect the path — the filename does.
        See `ArtifactKind` for the per-kind filename conventions. The
        runtime check below catches callers that accidentally pass a
        raw string instead of the enum member.

        Parents are created on demand. Rejects `..` and absolute paths
        in filename so a hostile / buggy caller can't write outside the
        run's artifact root.
        """
        self._validate(kind, filename)
        dest = (
            self._root
            / team_id
            / f"round-{round_id}"
            / filename
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    def team_path_for(
        self,
        kind: ArtifactKind,
        *,
        team_id: str,
        filename: str,
    ) -> Path:
        """Return runs/<ts>/artifacts/<team_id>/<filename> — NO round subdir.

        For async mode, where the agent runs continuously across the whole
        game and artifacts aren't per-round (chunk-numbered files like
        `stream-async-0.jsonl` live directly under the team dir).
        """
        self._validate(kind, filename)
        dest = self._root / team_id / filename
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    @staticmethod
    def _validate(kind: ArtifactKind, filename: str) -> None:
        """Shared guards: enum kind, no '..' / absolute filename (so a
        hostile / buggy caller can't write outside the run's artifact root)."""
        if not isinstance(kind, ArtifactKind):
            raise TypeError(
                f"kind must be an ArtifactKind, got {type(kind).__name__}: {kind!r}"
            )
        if ".." in Path(filename).parts:
            raise ValueError(f"filename must not contain '..': {filename!r}")
        if Path(filename).is_absolute():
            raise ValueError(f"filename must be relative: {filename!r}")

    @property
    def root(self) -> Path:
        """The artifacts/ directory inside this run. For tests + introspection."""
        return self._root
