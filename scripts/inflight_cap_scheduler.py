#!/usr/bin/env python3
"""Resident whole-match scheduler for mixed arena batch policies.

The policy is selected once, from the entire batch, before any submission:

* a batch containing zero ``dsh`` agents runs with exactly 20 resident slots;
* a batch containing any ``dsh`` agent also runs with exactly 20 resident slots.

A resident slot is released only when its job reaches a terminal status.  This
script is intentionally submission-side: it does not alter agent warmup,
readiness, images, or the orchestrator itself.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import shlex
import time
from typing import Callable
import urllib.request

import yaml


DEFAULT_API = "http://127.0.0.1:20000/api/jobs"
ZERO_DSH_CAP = 20
TERMINAL = {"succeeded", "failed", "timed_out", "canceled"}


def config_has_dsh(path: str | os.PathLike[str]) -> bool:
    """Return whether any team in one config uses the dsh CLI.

    Parse the YAML structure rather than searching text so comments, model
    names, and unrelated strings containing ``dsh`` cannot change policy.
    """
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict) or not isinstance(data.get("teams"), list):
        raise ValueError(f"invalid arena config structure: {path}")
    for team in data["teams"]:
        if not isinstance(team, dict) or not isinstance(team.get("agent"), dict):
            raise ValueError(f"invalid team/agent structure: {path}")
        agent = team["agent"]
        cli = str(agent.get("cli") or "")
        try:
            command = shlex.split(cli)
        except ValueError:
            command = cli.split()
        if command and os.path.basename(command[0]) == "dsh":
            return True
    return False


def batch_has_dsh(configs: list[str]) -> bool:
    """Classify the complete batch before scheduling starts."""
    return any(config_has_dsh(path) for path in configs)


def select_cap(configs: list[str], dsh_cap: int) -> tuple[int, str]:
    """Return the immutable resident cap and selected policy name."""
    if dsh_cap < 1:
        raise ValueError("dsh cap must be positive")
    if batch_has_dsh(configs):
        return ZERO_DSH_CAP, "dsh-cap20"
    return ZERO_DSH_CAP, "zero-dsh-cap20"


def terminal_jobs(statuses: dict[str, str]) -> list[str]:
    """Only true whole-match terminal states release resident slots."""
    return [job_id for job_id, state in statuses.items() if state in TERMINAL]


def _label(path: str) -> tuple[str, str]:
    base = Path(path).stem
    if "__" in base:
        return tuple(base.rsplit("__", 1))  # type: ignore[return-value]
    return base, "default"


def run(
    configs: list[str],
    dsh_cap: int,
    out_path: str,
    log_path: str,
    *,
    api: str = DEFAULT_API,
    poll_secs: float = 20.0,
    load_reader: Callable[[], float] | None = None,
    adopted_active: dict[str, str] | None = None,
    evidence_path: str | None = None,
) -> tuple[int, float]:
    """Run the resident scheduler and return observed peak inflight/load."""
    if not configs:
        raise ValueError("batch is empty")
    if poll_secs <= 0:
        raise ValueError("poll interval must be positive")
    if Path(out_path).resolve() == Path(log_path).resolve():
        raise ValueError("result and log paths must differ")
    if evidence_path is not None:
        evidence_resolved = Path(evidence_path).resolve()
        if evidence_resolved in {Path(out_path).resolve(), Path(log_path).resolve()}:
            raise ValueError("evidence, result, and log paths must differ")
    cap, policy = select_cap(configs, dsh_cap)
    adopted_active = adopted_active or {}
    by_name = {Path(path).name: path for path in configs}
    unknown_adopted = set(adopted_active) - set(by_name)
    if unknown_adopted:
        raise ValueError(f"adopted configs not in batch: {sorted(unknown_adopted)}")
    if len(set(adopted_active.values())) != len(adopted_active):
        raise ValueError("adopted active job ids must be unique")
    if len(adopted_active) > cap:
        raise ValueError("adopted active jobs exceed selected cap")
    queue = [path for path in configs if Path(path).name not in adopted_active]
    active: dict[str, tuple[str, str, str]] = {}
    Path(out_path).write_text("")
    Path(log_path).write_text("")
    if evidence_path is not None:
        Path(evidence_path).write_text("")

    def log(message: str) -> None:
        with open(log_path, "a") as handle:
            handle.write(f"{time.strftime('%H:%M:%S')} {message}\n")

    def evidence(kind: str, **fields: object) -> None:
        if evidence_path is None:
            return
        row = {
            "schema": "cyberarena.dsh.scheduler/v1",
            "kind": kind,
            "ts": time.time(),
            **fields,
        }
        with open(evidence_path, "a") as handle:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            handle.write("\n")

    def submit(path: str) -> str | None:
        req = urllib.request.Request(
            api,
            data=Path(path).read_bytes(),
            method="POST",
            headers={"Content-Type": "application/x-yaml"},
        )
        try:
            response = json.loads(urllib.request.urlopen(req, timeout=25).read())
            job_id = response.get("id") if isinstance(response, dict) else None
            if not isinstance(job_id, str) or not job_id.strip():
                raise ValueError("response omitted a valid job id")
            return job_id
        except Exception as exc:
            # A timed-out POST may already have committed server-side.  Never
            # retry an ambiguous submission: fail closed so actual resident
            # matches cannot silently exceed the cap.
            log(
                "SUBMIT_AMBIGUOUS_ABORT "
                f"config={Path(path).name} error_class={type(exc).__name__}"
            )
            evidence(
                "submit_ambiguous_abort",
                config=Path(path).name,
                error_class=type(exc).__name__,
            )
            raise RuntimeError(
                f"submission result is ambiguous for {Path(path).name}; not retrying"
            ) from exc

    def status(job_id: str) -> str:
        try:
            response = urllib.request.urlopen(f"{api}/{job_id}", timeout=8).read()
            return json.loads(response).get("status", "unknown")
        except Exception as exc:
            log(f"STATUS_UNKNOWN job={job_id} error_class={type(exc).__name__}")
            evidence("status_unknown", job_id=job_id, error_class=type(exc).__name__)
            return "unknown"

    def read_load() -> float:
        if load_reader is not None:
            return load_reader()
        try:
            return float(Path("/proc/loadavg").read_text().split()[0])
        except Exception:
            return -1.0

    peak_inflight = 0
    peak_load = -1.0
    initial_fill = True
    refill_after: list[str] = []
    terminal_count = 0
    log(
        f"START configs={len(configs)} policy={policy} cap={cap} "
        f"requested_dsh_cap={dsh_cap} "
        f"batch_has_dsh={str(batch_has_dsh(configs)).lower()} api={api}"
    )
    evidence(
        "batch_start",
        configs=len(configs),
        policy=policy,
        cap=cap,
        zero_dsh_cap=ZERO_DSH_CAP,
        requested_dsh_cap=dsh_cap,
        batch_has_dsh=batch_has_dsh(configs),
    )
    for path in configs:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        log(f"CONFIG path={Path(path).resolve()} sha256={digest}")
        evidence("config", config=Path(path).name, sha256=digest)
    for config_name, job_id in adopted_active.items():
        path = by_name[config_name]
        challenge, arm = _label(path)
        active[job_id] = (challenge, arm, path)
        log(
            f"ADOPT_ACTIVE config={config_name} job={job_id} "
            f"in_flight={len(active)}"
        )
        evidence(
            "adopt_active",
            config=config_name,
            job_id=job_id,
            in_flight=len(active),
        )

    while queue or active:
        while len(active) < cap and queue:
            path = queue.pop(0)
            challenge, arm = _label(path)
            job_id = submit(path)
            if not job_id:
                raise RuntimeError(f"server returned no job id for {Path(path).name}")
            if job_id in active:
                raise RuntimeError(f"server returned duplicate active job id: {job_id}")
            active[job_id] = (challenge, arm, path)
            reason = "INITIAL" if initial_fill else (
                f"REFILL_AFTER_TERMINAL={','.join(refill_after)}"
                if refill_after else "SUBMIT_RETRY"
            )
            log(
                f"SUBMIT reason={reason} config={Path(path).name} "
                f"job={job_id} in_flight={len(active)}"
            )
            evidence(
                "submit",
                config=Path(path).name,
                job_id=job_id,
                in_flight=len(active),
                queue=len(queue),
                reason="initial" if initial_fill else "refill_after_terminal",
                released_jobs=[] if initial_fill else list(refill_after),
            )
        initial_fill = False
        refill_after = []
        peak_inflight = max(peak_inflight, len(active))
        peak_load = max(peak_load, read_load())
        evidence(
            "snapshot",
            final=False,
            in_flight=len(active),
            queue=len(queue),
            terminal_count=terminal_count,
            peak_inflight=peak_inflight,
            peak_load=round(peak_load, 2),
        )

        if not active:
            continue
        time.sleep(poll_secs)
        statuses = {job_id: status(job_id) for job_id in active}
        refill_after = terminal_jobs(statuses)
        for job_id in refill_after:
            challenge, arm, path = active.pop(job_id)
            state = statuses[job_id]
            with open(out_path, "a") as handle:
                handle.write(f"{challenge}\t{arm}\t{job_id}\t{state}\n")
            log(
                f"TERMINAL challenge={challenge} arm={arm} job={job_id} "
                f"status={state} in_flight={len(active)} queue={len(queue)}"
            )
            terminal_count += 1
            evidence(
                "terminal",
                config=Path(path).name,
                job_id=job_id,
                status=state,
                in_flight=len(active),
                queue=len(queue),
                terminal_count=terminal_count,
            )

    log(f"DONE peak_inflight={peak_inflight} peak_load={peak_load:.2f}")
    evidence(
        "snapshot",
        final=True,
        in_flight=0,
        queue=0,
        terminal_count=terminal_count,
        peak_inflight=peak_inflight,
        peak_load=round(peak_load, 2),
    )
    evidence(
        "batch_done",
        terminal_count=terminal_count,
        peak_inflight=peak_inflight,
        peak_load=round(peak_load, 2),
    )
    print(f"peak_inflight={peak_inflight} peak_load={peak_load:.2f}")
    return peak_inflight, peak_load


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config_glob")
    parser.add_argument("dsh_cap", type=int)
    parser.add_argument("out_tsv")
    parser.add_argument("log")
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--poll-secs", type=float, default=20.0)
    parser.add_argument(
        "--evidence-jsonl",
        help="append secret-safe structured scheduler evidence (schema v1)",
    )
    parser.add_argument(
        "--adopt-active",
        action="append",
        default=[],
        metavar="CONFIG=JOB_ID",
        help="adopt an already-submitted config into resident accounting",
    )
    args = parser.parse_args()
    configs = sorted(glob.glob(args.config_glob))
    adopted = {}
    for value in args.adopt_active:
        if "=" not in value:
            parser.error("--adopt-active must be CONFIG=JOB_ID")
        config_name, job_id = value.split("=", 1)
        if not config_name or not job_id or config_name in adopted:
            parser.error("invalid or duplicate --adopt-active CONFIG=JOB_ID")
        adopted[config_name] = job_id
    run(
        configs,
        args.dsh_cap,
        args.out_tsv,
        args.log,
        api=args.api,
        poll_secs=args.poll_secs,
        adopted_active=adopted,
        evidence_path=args.evidence_jsonl,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
