"""Focused contract tests for the resident batch scheduler (Board #1340)."""

from pathlib import Path
import json
import sys

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from inflight_cap_scheduler import (  # noqa: E402
    TERMINAL,
    ZERO_DSH_CAP,
    batch_has_dsh,
    config_has_dsh,
    select_cap,
    terminal_jobs,
    run,
)


def _config(tmp_path: Path, name: str, cli: str, comment: str = "") -> str:
    path = tmp_path / name
    path.write_text(
        f"{comment}\nteams:\n"
        f"  - id: team1\n    agent:\n      cli: {cli!r}\n"
        "  - id: team2\n    agent:\n      cli: 'codex exec'\n"
    )
    return str(path)


def test_entire_zero_dsh_batch_selects_exactly_twenty(tmp_path):
    configs = [
        _config(tmp_path, "a.yaml", "claude -p"),
        _config(tmp_path, "b.yaml", "codex exec"),
    ]
    assert not batch_has_dsh(configs)
    assert select_cap(configs, dsh_cap=5) == (ZERO_DSH_CAP, "zero-dsh-cap20")
    assert ZERO_DSH_CAP == 20


def test_dsh_anywhere_in_entire_batch_selects_exactly_twenty(tmp_path):
    configs = [
        _config(tmp_path, "first.yaml", "codex exec"),
        _config(tmp_path, "last.yaml", "dsh"),
    ]
    assert batch_has_dsh(configs)
    assert select_cap(configs, dsh_cap=5) == (ZERO_DSH_CAP, "dsh-cap20")


def test_dsh_detection_is_structural_not_comment_or_model_text(tmp_path):
    path = _config(
        tmp_path,
        "not-dsh.yaml",
        "codex exec",
        comment="# dsh deepseek-v4-pro must not affect batch policy",
    )
    assert not config_has_dsh(path)


@pytest.mark.parametrize("cli", ["dshx", "codex exec dsh", "bash -c dsh"])
def test_dsh_near_misses_do_not_select_dsh_policy(tmp_path, cli):
    assert not config_has_dsh(_config(tmp_path, "near-miss.yaml", cli))


@pytest.mark.parametrize("state", sorted(TERMINAL))
def test_every_terminal_whole_match_state_releases_a_slot(state):
    assert terminal_jobs({"done": state, "live": "running"}) == ["done"]


@pytest.mark.parametrize("state", ["queued", "running", "unknown", "starting", ""])
def test_nonterminal_state_never_releases_a_slot(state):
    assert terminal_jobs({"live": state}) == []


def test_twenty_resident_and_twenty_first_submits_only_after_terminal(
    tmp_path, monkeypatch
):
    configs = [
        _config(tmp_path, f"challenge-{number}.yaml", "codex exec")
        for number in range(21)
    ]
    submitted = []

    class Response:
        def __init__(self, payload):
            import json

            self.payload = json.dumps(payload).encode()

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout):
        if getattr(request, "method", None) == "POST":
            job_id = f"job-{len(submitted) + 1}"
            submitted.append(job_id)
            return Response({"id": job_id})
        job_id = str(request).rsplit("/", 1)[-1]
        # The first poll frees exactly one slot.  Once job-21 has filled that
        # slot, every remaining whole match may finish.
        state = "succeeded" if job_id == "job-1" or len(submitted) == 21 else "running"
        return Response({"status": state})

    monkeypatch.setattr("inflight_cap_scheduler.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("inflight_cap_scheduler.time.sleep", lambda _: None)
    result = tmp_path / "result.tsv"
    log = tmp_path / "scheduler.log"
    evidence = tmp_path / "scheduler.evidence.jsonl"

    peak, _ = run(
        configs,
        dsh_cap=5,
        out_path=str(result),
        log_path=str(log),
        # Production forbids zero; use a tiny positive interval while sleep is
        # mocked out for this deterministic test.
        poll_secs=0.001,
        load_reader=lambda: 1.25,
        evidence_path=str(evidence),
    )

    lines = log.read_text().splitlines()
    terminal_index = next(i for i, line in enumerate(lines) if "job=job-1 " in line and "TERMINAL" in line)
    refill_index = next(i for i, line in enumerate(lines) if "job=job-21 " in line)
    assert terminal_index < refill_index
    assert "reason=REFILL_AFTER_TERMINAL=job-1" in lines[refill_index]
    assert peak == 20
    assert len(result.read_text().splitlines()) == 21
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert all(row["schema"] == "cyberarena.dsh.scheduler/v1" for row in rows)
    start = next(row for row in rows if row["kind"] == "batch_start")
    assert start["cap"] == 20
    assert start["zero_dsh_cap"] == 20
    assert start["policy"] == "zero-dsh-cap20"
    assert any(
        row["kind"] == "snapshot"
        and row["final"] is False
        and row["in_flight"] == 20
        and row["terminal_count"] == 0
        for row in rows
    )
    refill = next(
        row for row in rows
        if row["kind"] == "submit" and row["job_id"] == "job-21"
    )
    assert refill["reason"] == "refill_after_terminal"
    assert refill["released_jobs"] == ["job-1"]
    done = next(row for row in rows if row["kind"] == "batch_done")
    assert done["terminal_count"] == 21
    assert done["peak_inflight"] == 20


def test_adopted_jobs_count_toward_resident_cap(tmp_path, monkeypatch):
    configs = [
        _config(tmp_path, f"challenge-{number}.yaml", "codex exec")
        for number in range(21)
    ]
    submitted = []

    class Response:
        def __init__(self, payload):
            import json

            self.payload = json.dumps(payload).encode()

        def read(self):
            return self.payload

    def fake_urlopen(request, timeout):
        if getattr(request, "method", None) == "POST":
            job_id = f"new-{len(submitted) + 1}"
            submitted.append(job_id)
            return Response({"id": job_id})
        return Response({"status": "succeeded"})

    monkeypatch.setattr("inflight_cap_scheduler.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setattr("inflight_cap_scheduler.time.sleep", lambda _: None)
    result = tmp_path / "adopt-result.tsv"
    log = tmp_path / "adopt.log"
    peak, _ = run(
        configs,
        dsh_cap=5,
        out_path=str(result),
        log_path=str(log),
        poll_secs=0.001,
        load_reader=lambda: 1.0,
        adopted_active={"challenge-0.yaml": "existing-1", "challenge-1.yaml": "existing-2"},
    )
    assert peak == 20
    assert len(submitted) == 19
    assert "ADOPT_ACTIVE config=challenge-0.yaml job=existing-1" in log.read_text()


def test_evidence_path_must_not_overwrite_result_or_log(tmp_path):
    config = _config(tmp_path, "one.yaml", "codex exec")
    shared = tmp_path / "shared.out"
    with pytest.raises(ValueError, match="evidence, result, and log"):
        run(
            [config],
            dsh_cap=20,
            out_path=str(shared),
            log_path=str(tmp_path / "scheduler.log"),
            evidence_path=str(shared),
        )


def test_scheduler_error_evidence_does_not_persist_secret(tmp_path, monkeypatch):
    config = _config(tmp_path, "one.yaml", "dsh")
    secret = "proxy-credential-super-secret-value"

    def fake_urlopen(_request, timeout):
        assert timeout == 25
        raise RuntimeError(secret)

    monkeypatch.setattr("inflight_cap_scheduler.urllib.request.urlopen", fake_urlopen)
    result = tmp_path / "result.tsv"
    log = tmp_path / "scheduler.log"
    evidence = tmp_path / "scheduler.evidence.jsonl"

    with pytest.raises(RuntimeError, match="submission result is ambiguous"):
        run(
            [config],
            dsh_cap=20,
            out_path=str(result),
            log_path=str(log),
            poll_secs=0.001,
            evidence_path=str(evidence),
        )

    persisted = log.read_text() + evidence.read_text()
    assert secret not in persisted
    assert "error_class=RuntimeError" in persisted
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    failure = next(row for row in rows if row["kind"] == "submit_ambiguous_abort")
    assert failure["error_class"] == "RuntimeError"


def test_missing_job_id_is_an_ambiguous_fail_closed_submission(tmp_path, monkeypatch):
    config = _config(tmp_path, "one.yaml", "dsh")

    class Response:
        def read(self):
            return b'{"status":"queued"}'

    monkeypatch.setattr(
        "inflight_cap_scheduler.urllib.request.urlopen",
        lambda _request, timeout: Response(),
    )
    evidence = tmp_path / "scheduler.evidence.jsonl"
    with pytest.raises(RuntimeError, match="submission result is ambiguous"):
        run(
            [config],
            dsh_cap=20,
            out_path=str(tmp_path / "result.tsv"),
            log_path=str(tmp_path / "scheduler.log"),
            poll_secs=0.001,
            evidence_path=str(evidence),
        )

    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    failure = next(row for row in rows if row["kind"] == "submit_ambiguous_abort")
    assert failure["error_class"] == "ValueError"
