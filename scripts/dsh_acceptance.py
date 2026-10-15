#!/usr/bin/env python3
"""Fail-closed validator for one resident dsh acceptance batch.

The input is a small JSON manifest that names scheduler evidence, results,
twenty configs, and one events.jsonl per job.  The output deliberately contains
only counts, hashes, closed status values, and timing summaries.  Raw config
values, proxy URLs, event payload errors, and credentials are never copied.
"""

from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import yaml


INPUT_SCHEMA = "cyberarena.dsh.acceptance-input/v1"
REPORT_SCHEMA = "cyberarena.dsh.acceptance-report/v1"
SCHEDULER_SCHEMA = "cyberarena.dsh.scheduler/v1"
EXPECTED_MATCHES = 20
EXPECTED_TEAMS = 2
EXPECTED_DSH_MODEL = "deepseek-v4-flash"
EXPECTED_DSH_PACKAGE = {
    "name": "@deepseek-ai/dsh",
    "version": "0.1.0-rc.6",
    "tarball": (
        "https://registry.npmjs.org/@deepseek-ai/dsh/-/"
        "dsh-0.1.0-rc.6.tgz"
    ),
    "integrity": (
        "sha512-brpZfED7ieRa2PQ5tUxMhHrM1pb2CmKFVM/f6yMULBDMicahk+Z2OsHgTwTDnoiZm23Ftu9rQz0NN4pflaoJcg=="
    ),
}
APT_DOMAINS = ("archive.ubuntu.com", "security.ubuntu.com")
TERMINAL = {"succeeded", "failed", "timed_out", "canceled"}
SETUP_STATUSES = {
    "ok",
    "configured_proxy_http_403",
    "auth_error",
    "timeout",
    "exception",
    "command_error",
}
READINESS_FAILURE_CLASSES = {
    "configured_proxy_http_403",
    "auth_error",
    "timeout",
    "handler_exception",
    "agent_process_exit",
    "model_refusal",
    "agent_ready_missing",
}


class EvidenceError(ValueError):
    """Malformed or missing evidence; messages must not include raw content."""


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"cannot read valid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise EvidenceError(f"JSON root must be an object: {path.name}")
    return value


def _jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise EvidenceError(f"cannot read JSONL: {path.name}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvidenceError(
                f"invalid JSONL row {number}: {path.name}"
            ) from exc
        if not isinstance(row, dict):
            raise EvidenceError(f"JSONL row {number} is not an object: {path.name}")
        rows.append(row)
    if not rows:
        raise EvidenceError(f"empty JSONL: {path.name}")
    return rows


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceError(f"cannot hash file: {path.name}") from exc
    return digest.hexdigest()


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _timing(values: Iterable[float]) -> dict:
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return {"count": 0, "min": None, "median": None, "max": None}
    return {
        "count": len(numbers),
        "min": round(numbers[0], 3),
        "median": round(statistics.median(numbers), 3),
        "max": round(numbers[-1], 3),
    }


def _domain_allowed(host: str, patterns: Iterable[object]) -> bool:
    for raw in patterns:
        pattern = str(raw or "").strip().lower()
        if pattern and (host == pattern or fnmatch.fnmatch(host, pattern)):
            return True
    return False


def _proxy_shape(value: object) -> tuple[str, str, int | None] | None:
    """Return non-secret proxy shape; userinfo and path are discarded."""
    try:
        parsed = urlsplit(str(value or ""))
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.scheme, parsed.hostname.lower(), port


def validate_config(path: Path) -> dict:
    """Return a credential-free structural summary for one batch config."""
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise EvidenceError(f"invalid YAML config: {path.name}") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"config root must be a mapping: {path.name}")
    teams = data.get("teams")
    nodes = data.get("nodes")
    if not isinstance(teams, list) or not isinstance(nodes, list):
        raise EvidenceError(f"config missing teams/nodes lists: {path.name}")
    nodes_by_id = {
        str(node.get("id")): node
        for node in nodes
        if isinstance(node, dict) and node.get("id")
    }
    dsh_teams = []
    dsh_models = []
    proxy_shapes = []
    for team in teams:
        if not isinstance(team, dict) or not isinstance(team.get("agent"), dict):
            raise EvidenceError(f"invalid team agent structure: {path.name}")
        agent = team["agent"]
        if (
            str(agent.get("provider") or "").strip() == "deepseek"
            and str(agent.get("cli") or "").strip() == "dsh"
        ):
            dsh_teams.append(str(team.get("id") or ""))
            dsh_models.append(str(agent.get("model") or "").strip())
            dev_id = str(team.get("dev") or "")
            node = nodes_by_id.get(dev_id)
            if not isinstance(node, dict):
                raise EvidenceError(f"dsh dev node missing: {path.name}")
            env = node.get("env")
            if not isinstance(env, dict):
                raise EvidenceError(f"dsh dev proxy env missing: {path.name}")
            values = [
                env.get(name)
                for name in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
            ]
            shapes = [_proxy_shape(value) for value in values]
            if any(shape is None for shape in shapes):
                raise EvidenceError(f"dsh dev proxy env incomplete: {path.name}")
            proxy_shapes.extend(shape for shape in shapes if shape is not None)
    api_router = data.get("api_router")
    if not isinstance(api_router, dict):
        raise EvidenceError(f"api_router missing: {path.name}")
    firewall = api_router.get("firewall")
    allow = firewall.get("allow") if isinstance(firewall, dict) else None
    domains = allow.get("domains") if isinstance(allow, dict) else None
    if not isinstance(domains, list):
        domains = []
    return {
        "name": path.name,
        "sha256": _sha256(path),
        "team_count": len(teams),
        "dsh_team_count": len(dsh_teams),
        "dsh_models_exact": bool(dsh_models) and all(
            model == EXPECTED_DSH_MODEL for model in dsh_models
        ),
        "proxy_complete": bool(proxy_shapes) and len(set(proxy_shapes)) == 1,
        "apt_domains_allowed": all(
            _domain_allowed(host, domains) for host in APT_DOMAINS
        ),
    }


def _results(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise EvidenceError(f"cannot read results: {path.name}") from exc
    for number, line in enumerate(lines, 1):
        parts = line.split("\t")
        if len(parts) != 4:
            raise EvidenceError(f"invalid results row {number}: {path.name}")
        _challenge, _arm, job_id, status = parts
        if not job_id or job_id in rows or status not in TERMINAL:
            raise EvidenceError(f"invalid results identity/status: {path.name}")
        rows[job_id] = status
    return rows


def _scheduler(rows: list[dict]) -> dict:
    if any(row.get("schema") != SCHEDULER_SCHEMA for row in rows):
        raise EvidenceError("scheduler schema mismatch")
    starts = [row for row in rows if row.get("kind") == "batch_start"]
    done = [row for row in rows if row.get("kind") == "batch_done"]
    if len(starts) != 1 or len(done) != 1:
        raise EvidenceError("scheduler must contain one batch_start and batch_done")
    start, final = starts[0], done[0]
    cap = start.get("cap")
    terminal_seen: set[str] = set()
    submitted_seen: set[str] = set()
    active: set[str] = set()
    refill_count = 0
    refill_valid = True
    accounting_valid = isinstance(cap, int) and not isinstance(cap, bool) and cap > 0
    peak_observed = 0
    for row in rows:
        kind = row.get("kind")
        if kind == "adopt_active":
            job_id = str(row.get("job_id") or "")
            if not job_id or job_id in submitted_seen:
                accounting_valid = False
            else:
                submitted_seen.add(job_id)
                active.add(job_id)
            if row.get("in_flight") != len(active):
                accounting_valid = False
        elif kind == "terminal":
            job_id = str(row.get("job_id") or "")
            if (
                not job_id
                or job_id not in active
                or job_id in terminal_seen
                or row.get("status") not in TERMINAL
            ):
                refill_valid = False
                accounting_valid = False
            active.discard(job_id)
            terminal_seen.add(job_id)
            if (
                row.get("in_flight") != len(active)
                or row.get("terminal_count") != len(terminal_seen)
            ):
                accounting_valid = False
        elif kind == "submit":
            job_id = str(row.get("job_id") or "")
            reason = row.get("reason")
            released = row.get("released_jobs")
            if reason == "refill_after_terminal":
                refill_count += 1
                if (
                    not isinstance(released, list)
                    or not released
                    or not set(map(str, released)) <= terminal_seen
                ):
                    refill_valid = False
            elif reason != "initial":
                refill_valid = False
            if not job_id or job_id in submitted_seen:
                accounting_valid = False
            else:
                submitted_seen.add(job_id)
                active.add(job_id)
            if row.get("in_flight") != len(active):
                accounting_valid = False
        elif kind == "snapshot":
            if row.get("in_flight") != len(active):
                accounting_valid = False
        if kind in {"adopt_active", "submit", "terminal", "snapshot"}:
            peak_observed = max(peak_observed, len(active))
            if isinstance(cap, int) and len(active) > cap:
                accounting_valid = False
    non_final_peak = any(
        row.get("kind") == "snapshot"
        and row.get("final") is False
        and row.get("in_flight") == EXPECTED_MATCHES
        and row.get("terminal_count") == 0
        for row in rows
    )
    unsafe = [
        row for row in rows
        if row.get("kind") in {"submit_ambiguous_abort", "status_unknown"}
    ]
    config_rows = [row for row in rows if row.get("kind") == "config"]
    configs = {
        str(row.get("config")): str(row.get("sha256")) for row in config_rows
    }
    if len(configs) != len(config_rows):
        accounting_valid = False
    if (
        active
        or final.get("terminal_count") != len(terminal_seen)
        or final.get("peak_inflight") != peak_observed
    ):
        accounting_valid = False
    return {
        "start": start,
        "done": final,
        "config_hashes": configs,
        "terminal_seen": terminal_seen,
        "refill_count": refill_count,
        "refill_valid": refill_valid,
        "accounting_valid": accounting_valid,
        "non_final_peak": non_final_peak,
        "unsafe_count": len(unsafe),
    }


def _job_events(path: Path) -> dict:
    events = _jsonl(path)
    setup_started = [e for e in events if e.get("type") == "SETUP_STARTED"]
    setup_complete = [e for e in events if e.get("type") == "SETUP_COMPLETE"]
    ready = [e for e in events if e.get("type") == "AGENT_READY"]
    dry_ok = [e for e in events if e.get("type") == "DRY_RUN_OK"]
    dry_failed = [e for e in events if e.get("type") == "DRY_RUN_FAILED"]
    if len(setup_started) == 1 and len(setup_complete) == 1:
        payload = setup_complete[0].get("payload")
        explicit = (
            _finite_number(payload.get("duration_secs"))
            if isinstance(payload, dict)
            else None
        )
        if explicit is None:
            start_ts = _finite_number(setup_started[0].get("ts"))
            end_ts = _finite_number(setup_complete[0].get("ts"))
            setup_duration = (
                max(0.0, end_ts - start_ts)
                if start_ts is not None and end_ts is not None
                else None
            )
        else:
            setup_duration = explicit
    else:
        setup_duration = None
    setup_payload = (
        setup_complete[0].get("payload")
        if len(setup_complete) == 1
        else {}
    )
    dev_setup = (
        setup_payload.get("dev_setup")
        if isinstance(setup_payload, dict)
        else {}
    )
    if not isinstance(dev_setup, dict):
        dev_setup = {}
    setup_statuses = [
        result.get("status")
        for result in dev_setup.values()
        if isinstance(result, dict)
    ]
    proxy_flags = [
        result.get("proxy_configured")
        for result in dev_setup.values()
        if isinstance(result, dict)
    ]
    readiness = []
    for event in dry_ok:
        payload = event.get("payload")
        duration = (
            _finite_number(payload.get("duration_secs"))
            if isinstance(payload, dict)
            else None
        )
        if duration is not None:
            readiness.append(duration)
    readiness_failure_classes = []
    for event in dry_failed:
        payload = event.get("payload")
        failure_class = (
            payload.get("failure_class") if isinstance(payload, dict) else None
        )
        readiness_failure_classes.append(failure_class)
    return {
        "setup_complete": len(setup_complete),
        "setup_duration": setup_duration,
        "agent_ready": len(ready),
        "ready_teams": len({e.get("team_id") for e in ready if e.get("team_id")}),
        "dry_run_ok": len(dry_ok),
        "dry_run_ok_teams": len(
            {e.get("team_id") for e in dry_ok if e.get("team_id")}
        ),
        "dry_run_failed": len(dry_failed),
        "readiness_durations": readiness,
        "setup_statuses": setup_statuses,
        "proxy_flags": proxy_flags,
        "readiness_failure_classes": readiness_failure_classes,
    }


def evaluate(input_path: Path) -> dict:
    manifest = _json(input_path)
    if manifest.get("schema") != INPUT_SCHEMA:
        raise EvidenceError("acceptance input schema mismatch")
    root = input_path.parent

    def resolved(name: str) -> Path:
        value = manifest.get(name)
        if not isinstance(value, str) or not value:
            raise EvidenceError(f"missing path field: {name}")
        return (root / value).resolve()

    scheduler_rows = _jsonl(resolved("scheduler_evidence"))
    scheduler = _scheduler(scheduler_rows)
    results = _results(resolved("results_tsv"))
    config_values = manifest.get("configs")
    job_values = manifest.get("jobs")
    provenance = manifest.get("provenance")
    if not isinstance(config_values, list) or not isinstance(job_values, list):
        raise EvidenceError("configs/jobs must be lists")
    if not isinstance(provenance, dict):
        raise EvidenceError("provenance must be an object")

    config_summaries = [
        validate_config((root / str(value)).resolve())
        for value in config_values
    ]
    jobs: dict[str, dict] = {}
    for item in job_values:
        if not isinstance(item, dict):
            raise EvidenceError("job entry must be an object")
        job_id = str(item.get("job_id") or "")
        events_path = item.get("events")
        if not job_id or job_id in jobs or not isinstance(events_path, str):
            raise EvidenceError("invalid/duplicate job entry")
        jobs[job_id] = _job_events((root / events_path).resolve())

    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, observed: object, expected: object) -> None:
        checks[name] = {
            "ok": bool(ok),
            "observed": observed,
            "expected": expected,
        }

    config_names = [item["name"] for item in config_summaries]
    actual_hashes = {item["name"]: item["sha256"] for item in config_summaries}
    check("config_count", len(config_summaries) == EXPECTED_MATCHES,
          len(config_summaries), EXPECTED_MATCHES)
    check("config_names_unique", len(set(config_names)) == len(config_names),
          len(set(config_names)), len(config_names))
    check("config_dsh_shape",
          all(item["team_count"] == EXPECTED_TEAMS
              and item["dsh_team_count"] >= 1 for item in config_summaries),
          sum(item["dsh_team_count"] for item in config_summaries),
          f">={EXPECTED_MATCHES}")
    check("config_dsh_model",
          all(item["dsh_models_exact"] for item in config_summaries),
          sum(bool(item["dsh_models_exact"]) for item in config_summaries),
          EXPECTED_MATCHES)
    check("config_proxy_complete",
          all(item["proxy_complete"] for item in config_summaries),
          sum(bool(item["proxy_complete"]) for item in config_summaries),
          EXPECTED_MATCHES)
    check("config_proxy_allows_apt",
          all(item["apt_domains_allowed"] for item in config_summaries),
          sum(bool(item["apt_domains_allowed"]) for item in config_summaries),
          EXPECTED_MATCHES)
    check("scheduler_config_hashes",
          scheduler["config_hashes"] == actual_hashes,
          len(scheduler["config_hashes"]), len(actual_hashes))

    start = scheduler["start"]
    done = scheduler["done"]
    check("resident_cap",
          start.get("cap") == EXPECTED_MATCHES
          and done.get("peak_inflight") == EXPECTED_MATCHES,
          {"cap": start.get("cap"), "peak": done.get("peak_inflight")},
          {"cap": EXPECTED_MATCHES, "peak": EXPECTED_MATCHES})
    check("zero_dsh_cap_non_regression",
          start.get("zero_dsh_cap") == EXPECTED_MATCHES,
          start.get("zero_dsh_cap"), EXPECTED_MATCHES)
    check("terminal_only_refill", scheduler["refill_valid"],
          {"refills": scheduler["refill_count"]}, "all refills follow terminal")
    check("scheduler_accounting", scheduler["accounting_valid"],
          scheduler["accounting_valid"], True)
    check("non_final_peak_observed", scheduler["non_final_peak"],
          scheduler["non_final_peak"], True)
    check("peak_load_observed",
          _finite_number(done.get("peak_load")) is not None,
          {"present": _finite_number(done.get("peak_load")) is not None},
          {"present": True})
    check("scheduler_fail_closed", scheduler["unsafe_count"] == 0,
          scheduler["unsafe_count"], 0)

    expected_job_ids = set(results)
    check("job_evidence_identity", set(jobs) == expected_job_ids,
          len(jobs), len(expected_job_ids))
    check("terminal_count",
          len(results) == EXPECTED_MATCHES
          and scheduler["terminal_seen"] == expected_job_ids,
          len(results), EXPECTED_MATCHES)
    succeeded = sum(status == "succeeded" for status in results.values())
    check("final_outcomes", succeeded == EXPECTED_MATCHES,
          {"succeeded": succeeded, "total": len(results)},
          {"succeeded": EXPECTED_MATCHES, "total": EXPECTED_MATCHES})

    job_summaries = list(jobs.values())
    setup_count = sum(item["setup_complete"] for item in job_summaries)
    ready_count = sum(item["agent_ready"] for item in job_summaries)
    dry_ok_count = sum(item["dry_run_ok"] for item in job_summaries)
    dry_failed_count = sum(item["dry_run_failed"] for item in job_summaries)
    check("setup_complete", setup_count == EXPECTED_MATCHES,
          setup_count, EXPECTED_MATCHES)
    check("agent_ready",
          ready_count == EXPECTED_MATCHES * EXPECTED_TEAMS
          and all(item["ready_teams"] == EXPECTED_TEAMS for item in job_summaries),
          ready_count, EXPECTED_MATCHES * EXPECTED_TEAMS)
    check("dry_run_ok",
          dry_ok_count == EXPECTED_MATCHES * EXPECTED_TEAMS
          and all(item["dry_run_ok_teams"] == EXPECTED_TEAMS
                  for item in job_summaries),
          dry_ok_count, EXPECTED_MATCHES * EXPECTED_TEAMS)
    check("dry_run_failed", dry_failed_count == 0, dry_failed_count, 0)
    setup_statuses = [
        status for item in job_summaries for status in item["setup_statuses"]
    ]
    readiness_failure_classes = [
        failure_class
        for item in job_summaries
        for failure_class in item["readiness_failure_classes"]
    ]
    check("proxy_stage_classification",
          all(status in SETUP_STATUSES for status in setup_statuses)
          and all(failure_class in READINESS_FAILURE_CLASSES
                  for failure_class in readiness_failure_classes),
          {
              "setup_rows": len(setup_statuses),
              "readiness_failure_rows": len(readiness_failure_classes),
          },
          "closed setup/readiness vocabularies")
    check("proxy_setup_success",
          len(setup_statuses) == EXPECTED_MATCHES * EXPECTED_TEAMS
          and all(status == "ok" for status in setup_statuses)
          and all(flag is True
                  for item in job_summaries for flag in item["proxy_flags"]),
          {
              "ok": sum(status == "ok" for status in setup_statuses),
              "configured_proxy_http_403": sum(
                  status == "configured_proxy_http_403"
                  for status in setup_statuses
              ),
          },
          {"ok": EXPECTED_MATCHES * EXPECTED_TEAMS,
           "configured_proxy_http_403": 0})

    package = provenance.get("dsh_package")
    image = provenance.get("agent_image")
    package_ok = isinstance(package, dict) and all(
        package.get(key) == value for key, value in EXPECTED_DSH_PACKAGE.items()
    )
    image_digest = image.get("digest") if isinstance(image, dict) else None
    image_ok = (
        isinstance(image, dict)
        and isinstance(image.get("reference"), str)
        and bool(image.get("reference"))
        and isinstance(image_digest, str)
        and image_digest.startswith("sha256:")
        and len(image_digest) == 71
    )
    check("package_provenance", package_ok,
          {"name": package.get("name") if isinstance(package, dict) else None,
           "version": package.get("version") if isinstance(package, dict) else None},
          {"name": EXPECTED_DSH_PACKAGE["name"],
           "version": EXPECTED_DSH_PACKAGE["version"]})
    check("agent_image_provenance", image_ok,
          {"digest_present": bool(image_digest)}, {"digest_present": True})

    setup_durations = [
        item["setup_duration"]
        for item in job_summaries
        if item["setup_duration"] is not None
    ]
    readiness_durations = [
        duration
        for item in job_summaries
        for duration in item["readiness_durations"]
    ]
    check("setup_timing_complete",
          len(setup_durations) == EXPECTED_MATCHES,
          len(setup_durations), EXPECTED_MATCHES)
    check("readiness_timing_complete",
          len(readiness_durations) == EXPECTED_MATCHES * EXPECTED_TEAMS,
          len(readiness_durations), EXPECTED_MATCHES * EXPECTED_TEAMS)

    return {
        "schema": REPORT_SCHEMA,
        "accepted": all(item["ok"] for item in checks.values()),
        "checks": checks,
        "metrics": {
            "setup_secs": _timing(setup_durations),
            "readiness_secs": _timing(readiness_durations),
            "resident_peak": done.get("peak_inflight"),
            "peak_load": done.get("peak_load"),
            "refills": scheduler["refill_count"],
            "proxy_stage": {
                "setup": dict(sorted(Counter(setup_statuses).items())),
                "readiness_failure": dict(
                    sorted(Counter(readiness_failure_classes).items())
                ),
            },
        },
        "provenance": {
            "config_sha256": dict(sorted(actual_hashes.items())),
            "dsh_package": {
                "name": package.get("name") if isinstance(package, dict) else None,
                "version": package.get("version") if isinstance(package, dict) else None,
                "tarball": package.get("tarball") if isinstance(package, dict) else None,
                "integrity": package.get("integrity") if isinstance(package, dict) else None,
            },
            "agent_image": {
                "reference": image.get("reference") if isinstance(image, dict) else None,
                "digest": image_digest,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_manifest")
    parser.add_argument("output_report")
    args = parser.parse_args()
    output = Path(args.output_report)
    try:
        report = evaluate(Path(args.input_manifest))
    except EvidenceError as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "accepted": False,
            "fatal_error": str(exc),
        }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "schema": REPORT_SCHEMA,
        "accepted": report["accepted"],
        "output": str(output),
    }, sort_keys=True))
    return 0 if report["accepted"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
