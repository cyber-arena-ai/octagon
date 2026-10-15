# DSH proxy-readiness acceptance evidence

This contract is deliberately split into two secret-safe evidence streams:

1. scripts/inflight_cap_scheduler.py with --evidence-jsonl PATH appends
   cyberarena.dsh.scheduler/v1 rows while the batch runs.
2. Every match already persists events.jsonl. SETUP_COMPLETE now carries only
   structured dev-setup outcomes, and DRY_RUN_OK / DRY_RUN_FAILED carry
   attempts, duration, and a closed failure class.

No stream contains proxy URLs, credentials, headers, command output, or raw
exception messages.

## Dev-setup and readiness classes

Dev setup emits one of:

- ok
- configured_proxy_http_403
- timeout
- auth_error
- exception
- command_error

configured_proxy_http_403 is used only when a proxy is actually configured on
the dev node and setup output reports HTTP 403. An arbitrary 403 without a
configured proxy remains command_error.

Dry-run failure emits one of:

- configured_proxy_http_403
- timeout
- auth_error
- handler_exception
- agent_ready_missing

Timeout, authentication, and handler exceptions take precedence over setup
correlation. They retain the existing fail-loud behavior and are never retried
as an ordinary missing-ready signal.

## Scheduler JSONL

Every row has schema cyberarena.dsh.scheduler/v1 plus kind and ts. Kinds are
batch_start, config, adopt_active, submit, snapshot, terminal, batch_done,
status_unknown, and submit_ambiguous_abort. Config rows contain filename and
SHA-256, not an absolute path. A refill submission contains the
already-terminal job ids that released its slots. Snapshots have an explicit
final boolean; a non-final snapshot at resident 20 is the early-load proof,
while batch_done carries resident peak and peak load.

Example:

    python scripts/inflight_cap_scheduler.py \
      'configs/*.yaml' 20 results.tsv scheduler.log \
      --evidence-jsonl scheduler.evidence.jsonl

## Acceptance input and report

The acceptance-input.json schema is cyberarena.dsh.acceptance-input/v1. It
contains:

- scheduler_evidence: relative path to scheduler.evidence.jsonl
- results_tsv: relative path to the terminal results
- configs: list of relative config paths
- jobs: list of objects with job_id and relative events path
- provenance.dsh_package: name, version, tarball, and npm integrity
- provenance.agent_image: image reference and sha256 digest

The required dsh package is @deepseek-ai/dsh 0.1.0-rc.6, tarball
https://registry.npmjs.org/@deepseek-ai/dsh/-/dsh-0.1.0-rc.6.tgz, integrity
sha512-brpZfED7ieRa2PQ5tUxMhHrM1pb2CmKFVM/f6yMULBDMicahk+Z2OsHgTwTDnoiZm23Ftu9rQz0NN4pflaoJcg==.

All paths are relative to the input manifest. The real acceptance batch must
list exactly 20 distinct config filenames and exactly 20 job event files.

Before any result can pass, every config must:

- have two teams and at least one provider deepseek, CLI dsh team;
- pin every dsh team to the exact model `deepseek-v4-flash`;
- give that team's dev node all four HTTP(S) proxy environment variants,
  consistently pointing at one valid proxy endpoint;
- allow archive.ubuntu.com and security.ubuntu.com through the api-router
  firewall.

Run:

    python scripts/dsh_acceptance.py acceptance-input.json acceptance-report.json

Exit status is zero only when the report schema is
cyberarena.dsh.acceptance-report/v1 and accepted is true. Otherwise it returns
2 and writes a fail-closed report.

The validator checks:

- setup complete 20/20;
- AGENT_READY 40/40;
- DRY_RUN_OK 40/40 and DRY_RUN_FAILED 0/40;
- configured-proxy setup success with zero proxy-403 classifications;
- resident cap and observed peak exactly 20;
- every refill follows a recorded whole-match terminal state;
- a non-final resident-20 snapshot before any terminal;
- final results 20/20 succeeded;
- complete setup/readiness timing distributions and peak load;
- exact dsh package provenance plus a digest-pinned agent image;
- exact config SHA-256 provenance and closed setup/readiness proxy-stage classes;
- the zero-dsh cap remains exactly 20.

The generated report includes only safe summaries: check booleans, counts,
hashes, enumerated statuses, timing min/median/max, and package/image
provenance. `metrics.proxy_stage` reports only counts keyed by those closed
status vocabularies; raw errors and proxy values are never copied.
