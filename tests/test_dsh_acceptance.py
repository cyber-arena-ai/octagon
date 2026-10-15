"""Deterministic acceptance report for one dsh resident-20 batch."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import yaml


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from dsh_acceptance import (  # noqa: E402
    EXPECTED_DSH_PACKAGE,
    INPUT_SCHEMA,
    READINESS_FAILURE_CLASSES,
    REPORT_SCHEMA,
    evaluate,
)


def _config() -> dict:
    proxy_env = {
        "HTTPS_PROXY": "http://user:must-not-appear@api-router:3128",
        "HTTP_PROXY": "http://user:must-not-appear@api-router:3128",
        "https_proxy": "http://user:must-not-appear@api-router:3128",
        "http_proxy": "http://user:must-not-appear@api-router:3128",
    }
    return {
        "teams": [
            {
                "id": "team1",
                "dev": "team1_dev",
                "prod": "team1_prod",
                "agent": {
                    "provider": "deepseek",
                    "cli": "dsh",
                    "model": "deepseek-v4-flash",
                },
            },
            {
                "id": "team2",
                "dev": "team2_dev",
                "prod": "team2_prod",
                "agent": {"provider": "codex", "cli": "codex exec"},
            },
        ],
        "nodes": [
            {"id": "team1_dev", "image": "agent", "networks": [], "env": proxy_env},
            {"id": "team1_prod", "image": "challenge", "networks": []},
            {"id": "team2_dev", "image": "agent", "networks": [], "env": proxy_env},
            {"id": "team2_prod", "image": "challenge", "networks": []},
        ],
        "api_router": {
            "firewall": {
                "default": "deny",
                "allow": {
                    "domains": [
                        "archive.ubuntu.com",
                        "security.ubuntu.com",
                    ]
                },
            }
        },
    }


def test_typed_command_failures_are_closed_readiness_failure_classes():
    assert {"agent_process_exit", "model_refusal"} <= READINESS_FAILURE_CLASSES


def _event(event_type: str, ts: float, *, team_id=None, payload=None) -> dict:
    return {
        "id": f"id-{event_type}-{team_id}-{ts}",
        "ts": ts,
        "type": event_type,
        "team_id": team_id,
        "round_id": 0,
        "phase": "",
        "payload": payload or {},
    }


def _write_fixture(tmp_path: Path, *, proxy_status: str = "ok") -> Path:
    configs = tmp_path / "configs"
    events_root = tmp_path / "events"
    configs.mkdir()
    events_root.mkdir()
    config_paths = []
    config_hashes = {}
    for number in range(1, 21):
        path = configs / f"match-{number:02d}.yaml"
        path.write_text(yaml.safe_dump(_config(), sort_keys=False))
        config_paths.append(str(path.relative_to(tmp_path)))
        config_hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()

    scheduler_rows = [{
        "schema": "cyberarena.dsh.scheduler/v1",
        "kind": "batch_start",
        "ts": 1.0,
        "configs": 20,
        "policy": "dsh-cap20",
        "cap": 20,
        "zero_dsh_cap": 20,
        "requested_dsh_cap": 20,
        "batch_has_dsh": True,
    }]
    for name, digest in config_hashes.items():
        scheduler_rows.append({
            "schema": "cyberarena.dsh.scheduler/v1",
            "kind": "config",
            "ts": 1.0,
            "config": name,
            "sha256": digest,
        })
    for number, name in enumerate(config_hashes, 1):
        scheduler_rows.append({
            "schema": "cyberarena.dsh.scheduler/v1",
            "kind": "submit",
            "ts": 2.0,
            "config": name,
            "job_id": f"job-{number:02d}",
            "in_flight": number,
            "queue": 20 - number,
            "reason": "initial",
            "released_jobs": [],
        })
    scheduler_rows.append({
        "schema": "cyberarena.dsh.scheduler/v1",
        "kind": "snapshot",
        "ts": 3.0,
        "final": False,
        "in_flight": 20,
        "queue": 0,
        "terminal_count": 0,
        "peak_inflight": 20,
        "peak_load": 9.5,
    })

    results = []
    jobs = []
    for number in range(1, 21):
        job_id = f"job-{number:02d}"
        scheduler_rows.append({
            "schema": "cyberarena.dsh.scheduler/v1",
            "kind": "terminal",
            "ts": 1000.0 + number,
            "config": f"match-{number:02d}.yaml",
            "job_id": job_id,
            "status": "succeeded",
            "in_flight": 20 - number,
            "queue": 0,
            "terminal_count": number,
        })
        results.append(f"challenge\tdefault\t{job_id}\tsucceeded\n")
        job_dir = events_root / job_id
        job_dir.mkdir()
        events = [
            _event("SETUP_STARTED", 10.0),
            _event(
                "SETUP_COMPLETE",
                16.0 + number / 10,
                payload={
                    "duration_secs": 6.0 + number / 10,
                    "dev_setup": {
                        "team1_dev": {
                            "status": proxy_status,
                            "duration_secs": 1.2,
                            "proxy_configured": True,
                            "rc": 0 if proxy_status == "ok" else 100,
                        },
                        "team2_dev": {
                            "status": "ok",
                            "duration_secs": 1.0,
                            "proxy_configured": True,
                            "rc": 0,
                        },
                    },
                },
            ),
            _event("DRY_RUN_START", 20.0, team_id="team1"),
            _event("AGENT_READY", 22.0, team_id="team1"),
            _event(
                "DRY_RUN_OK",
                22.1,
                team_id="team1",
                payload={"duration_secs": 2.1, "attempts": 1},
            ),
            _event("DRY_RUN_START", 20.0, team_id="team2"),
            _event("AGENT_READY", 21.5, team_id="team2"),
            _event(
                "DRY_RUN_OK",
                21.6,
                team_id="team2",
                payload={"duration_secs": 1.6, "attempts": 1},
            ),
        ]
        events_path = job_dir / "events.jsonl"
        events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        jobs.append({
            "job_id": job_id,
            "events": str(events_path.relative_to(tmp_path)),
        })

    scheduler_rows.extend([
        {
            "schema": "cyberarena.dsh.scheduler/v1",
            "kind": "snapshot",
            "ts": 1100.0,
            "final": True,
            "in_flight": 0,
            "queue": 0,
            "terminal_count": 20,
            "peak_inflight": 20,
            "peak_load": 9.5,
        },
        {
            "schema": "cyberarena.dsh.scheduler/v1",
            "kind": "batch_done",
            "ts": 1100.0,
            "terminal_count": 20,
            "peak_inflight": 20,
            "peak_load": 9.5,
        },
    ])
    scheduler_path = tmp_path / "scheduler.evidence.jsonl"
    scheduler_path.write_text(
        "".join(json.dumps(row) + "\n" for row in scheduler_rows)
    )
    results_path = tmp_path / "results.tsv"
    results_path.write_text("".join(results))
    manifest = {
        "schema": INPUT_SCHEMA,
        "scheduler_evidence": scheduler_path.name,
        "results_tsv": results_path.name,
        "configs": config_paths,
        "jobs": jobs,
        "provenance": {
            "dsh_package": EXPECTED_DSH_PACKAGE,
            "agent_image": {
                "reference": "cyberarena/agent",
                "digest": "sha256:" + "a" * 64,
            },
        },
    }
    manifest_path = tmp_path / "acceptance-input.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def test_healthy_resident_twenty_batch_is_accepted(tmp_path):
    report = evaluate(_write_fixture(tmp_path))
    assert report["schema"] == REPORT_SCHEMA
    assert report["accepted"] is True
    assert all(check["ok"] for check in report["checks"].values())
    assert report["metrics"]["resident_peak"] == 20
    assert report["metrics"]["peak_load"] == 9.5
    assert report["metrics"]["setup_secs"]["count"] == 20
    assert report["metrics"]["readiness_secs"]["count"] == 40
    assert report["metrics"]["proxy_stage"] == {
        "setup": {"ok": 40},
        "readiness_failure": {},
    }
    assert len(report["provenance"]["config_sha256"]) == 20
    rendered = json.dumps(report)
    assert "must-not-appear" not in rendered


def test_configured_proxy_403_is_distinct_and_fails_closed(tmp_path):
    report = evaluate(
        _write_fixture(tmp_path, proxy_status="configured_proxy_http_403")
    )
    assert report["accepted"] is False
    proxy = report["checks"]["proxy_setup_success"]
    assert proxy["ok"] is False
    assert proxy["observed"]["configured_proxy_http_403"] == 20


def test_missing_apt_allowlist_fails_config_validation_without_proxy_value(tmp_path):
    manifest_path = _write_fixture(tmp_path)
    config_path = tmp_path / "configs" / "match-01.yaml"
    data = yaml.safe_load(config_path.read_text())
    data["api_router"]["firewall"]["allow"]["domains"] = []
    config_path.write_text(yaml.safe_dump(data, sort_keys=False))
    # Keep scheduler evidence honest about the mutated config hash.
    rows = [
        json.loads(line)
        for line in (tmp_path / "scheduler.evidence.jsonl").read_text().splitlines()
    ]
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    for row in rows:
        if row.get("kind") == "config" and row.get("config") == config_path.name:
            row["sha256"] = digest
    (tmp_path / "scheduler.evidence.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    report = evaluate(manifest_path)
    assert report["accepted"] is False
    assert report["checks"]["config_proxy_allows_apt"]["ok"] is False
    assert "must-not-appear" not in json.dumps(report)


def test_non_exact_dsh_model_fails_closed_with_only_safe_model_check(tmp_path):
    manifest_path = _write_fixture(tmp_path)
    config_path = tmp_path / "configs" / "match-01.yaml"
    data = yaml.safe_load(config_path.read_text())
    data["teams"][0]["agent"]["model"] = "deepseek-v4"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False))
    rows = [
        json.loads(line)
        for line in (tmp_path / "scheduler.evidence.jsonl").read_text().splitlines()
    ]
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    for row in rows:
        if row.get("kind") == "config" and row.get("config") == config_path.name:
            row["sha256"] = digest
    (tmp_path / "scheduler.evidence.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )

    report = evaluate(manifest_path)
    assert report["accepted"] is False
    assert report["checks"]["config_dsh_model"]["ok"] is False


def test_refill_without_prior_terminal_fails_closed(tmp_path):
    manifest_path = _write_fixture(tmp_path)
    scheduler_path = tmp_path / "scheduler.evidence.jsonl"
    rows = [json.loads(line) for line in scheduler_path.read_text().splitlines()]
    submit = next(row for row in rows if row.get("kind") == "submit")
    submit["reason"] = "refill_after_terminal"
    submit["released_jobs"] = ["not-terminal"]
    scheduler_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    report = evaluate(manifest_path)
    assert report["accepted"] is False
    assert report["checks"]["terminal_only_refill"]["ok"] is False


def test_inconsistent_scheduler_resident_accounting_fails_closed(tmp_path):
    manifest_path = _write_fixture(tmp_path)
    scheduler_path = tmp_path / "scheduler.evidence.jsonl"
    rows = [json.loads(line) for line in scheduler_path.read_text().splitlines()]
    twentieth = [row for row in rows if row.get("kind") == "submit"][19]
    twentieth["in_flight"] = 21
    scheduler_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )

    report = evaluate(manifest_path)
    assert report["accepted"] is False
    assert report["checks"]["scheduler_accounting"]["ok"] is False
