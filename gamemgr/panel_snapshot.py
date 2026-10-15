"""Static panel snapshot writer.

After the round loop ends, embed the run's `events.jsonl` into a copy
of `panel/panel.html` and write it to `runs/<ts>/panel.html`. The
result is a self-contained file that re-renders the final state of
the live panel — no server, no WebSocket, no `/game/status` fetch.

The trick is on the panel side: if `window.INITIAL_EVENTS` is defined
(an array), the boot block replays it and skips the live connection.
`window.INITIAL_TRANSCRIPTS` is the same idea for the agent-transcript
section (which is otherwise fed by the live `/game/agents` endpoint).
Here we just inject one `<script>` tag right before `</body>`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .transcripts import collect_transcripts
from .trajectory import merge_by_team

log = logging.getLogger(__name__)

_PANEL_TEMPLATE = Path(__file__).parent.parent / "panel" / "panel.html"


def write_snapshot(run_dir: Path, out_path: Path | None = None) -> Path:
    """Read `run_dir/events.jsonl`, embed as INITIAL_EVENTS into a
    snapshot copy of `panel.html`, write to `out_path` (defaults to
    `run_dir/panel.html`).

    Returns the path written. Best-effort on the events side: any line
    that's not valid JSON is skipped with a warning. Missing
    `events.jsonl` is OK — snapshot still writes with an empty array,
    so the user can open the file and see "no events captured".
    """
    if out_path is None:
        out_path = run_dir / "panel.html"

    events: list[dict] = []
    events_path = run_dir / "events.jsonl"
    if events_path.is_file():
        for i, line in enumerate(events_path.read_text().splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning("panel_snapshot: skipping malformed line %d", i)
    else:
        log.warning("panel_snapshot: %s missing; writing empty snapshot", events_path)

    # Parse the run's agent transcripts so the offline snapshot shows the
    # same card stack the live panel does — merged one-per-team, matching
    # /game/agents. Best-effort: a parse failure must not cost us the (more
    # important) events snapshot.
    try:
        transcripts = merge_by_team(collect_transcripts(run_dir))
    except Exception:
        log.exception("panel_snapshot: transcript collection failed; embedding none")
        transcripts = []

    # Timeline metrics: embed artifacts/metrics.json so the offline snapshot's
    # "Timeline metrics" section renders too (the live panel fetches
    # /game/metrics, which isn't available offline). Written at game end before
    # this snapshot; best-effort — missing/bad = the section just stays hidden.
    metrics: dict = {}
    metrics_path = run_dir / "artifacts" / "metrics.json"
    if metrics_path.is_file():
        try:
            metrics = json.loads(metrics_path.read_text())
        except Exception:
            log.warning("panel_snapshot: metrics.json unreadable; embedding none")

    template = _PANEL_TEMPLATE.read_text()
    # Inject before the LAST </body> in the template. The first match
    # might be inside an HTML comment or JS string — bug-bait that
    # already cost one debug session. `rfind` pins it to the real
    # closing tag at end-of-document.
    inject_at = template.rfind("</body>")
    if inject_at < 0:
        raise RuntimeError(
            f"panel template at {_PANEL_TEMPLATE} missing </body>; cannot inject"
        )

    # `json.dumps` handles HTML-safe escaping for the data values; we
    # additionally close any `</script>` literal that could appear in a
    # payload string. (Browsers terminate inline <script> on the literal
    # bytes `</script>` regardless of JSON quoting.)
    events_payload = json.dumps(events).replace("</", "<\\/")
    transcripts_payload = json.dumps(transcripts).replace("</", "<\\/")
    metrics_payload = json.dumps(metrics).replace("</", "<\\/")
    injection = (
        '<script id="snapshot-events">\n'
        f"window.INITIAL_EVENTS = {events_payload};\n"
        f"window.INITIAL_TRANSCRIPTS = {transcripts_payload};\n"
        f"window.INITIAL_METRICS = {metrics_payload};\n"
        "</script>\n"
    )
    snapshot = template[:inject_at] + injection + template[inject_at:]

    out_path.write_text(snapshot)
    log.info(
        "panel snapshot written: %s (%d events, %d transcripts, %d metric team(s))",
        out_path, len(events), len(transcripts), len(metrics),
    )
    return out_path
