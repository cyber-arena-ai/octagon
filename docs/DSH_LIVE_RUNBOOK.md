# DSH live-run release and troubleshooting

This runbook covers the DeepSeek Harness (`provider: deepseek`, `cli: dsh`)
release path and the order for diagnosing a live run that does not start. Do not
paste credentials, environment values, request headers, or raw trajectories into
an issue. Report variable presence, immutable digests, closed event names, and
redacted log summaries only.

## Build and publish the agent image

A merged orche commit does not update an already-built agent image. Build from
the exact merge commit and publish an immutable registry digest.

From a clean checkout of the merged commit:

```bash
MERGE_SHA="$(git rev-parse HEAD)"
git status --short
git checkout --detach "$MERGE_SHA"

IMAGE_REF="registry.example.com/cyberarena/agent:dsh-${MERGE_SHA}"
docker build \
  -f dockerfiles/agent/Dockerfile \
  -t "$IMAGE_REF" \
  dockerfiles/agent

docker run --rm --entrypoint dsh "$IMAGE_REF" --version
# expected: 0.1.0-rc.6

docker image inspect "$IMAGE_REF" \
  --format 'Id={{.Id}} RepoDigests={{json .RepoDigests}}'
docker push "$IMAGE_REF"
docker pull "$IMAGE_REF"
docker image inspect "$IMAGE_REF" \
  --format 'Id={{.Id}} RepoDigests={{json .RepoDigests}}'
```

Use the `registry/name@sha256:<digest>` value returned after the push, not the
mutable tag, in every agent dev node:

```yaml
nodes:
  - id: team1_dev
    image: registry.example.com/cyberarena/agent@sha256:<digest>
```

On every runner that may claim the job, pull and verify that exact digest before
submission:

```bash
IMAGE="registry.example.com/cyberarena/agent@sha256:<digest>"
docker pull "$IMAGE"
docker image inspect "$IMAGE" --format '{{.Id}} {{json .RepoDigests}}'
docker run --rm --entrypoint dsh "$IMAGE" --version
```

Update the deployment-maintained YAML/template or runner configuration through
the deployment's normal reviewed rollout. Do not restart active matches to pick
up an image. New containers resolve the YAML image reference when they are
created; existing containers keep their current image.

`scripts/build_images.sh` builds local/runner images only. It does not push an
agent image, update a registry tag or digest, rewrite submitted YAML, restart a
worker, or refresh an existing container. Merging this PR therefore does not
refresh any deployed image automatically.

## DSH harness live-run troubleshooting

Run these checks in order. Stop at the first failed contract and fix that layer;
do not jump to provider quota while the local harness or routing path is broken.

### 1. Validate provider, CLI, and model

```bash
yq '.teams[] | {id, provider: .agent.provider, cli: .agent.cli, model: .agent.model}' game.yaml
```

Expected for every DSH team: `provider: deepseek`, `cli: dsh`, and a supported
non-empty model such as `deepseek-v4-flash`. Next, validate the complete YAML
with the same deploy/orche validator that will submit it. A mismatch here means
fix the YAML; do not build an image yet.

### 2. Record the code actually executed

```bash
git -C /path/to/orche rev-parse HEAD
git -C /path/to/orche submodule status challenge-warehouse
curl -fsS "$DEPLOY_URL/api/jobs/$JOB_ID" \
  | jq '{id,status,runner_id,orche_commit,warehouse_commit,exit_code,timed_out}'
```

Expected: the job's recorded orche/warehouse SHAs match the intended checkout.
If they differ, inspect the runner's `CAD_ORCHE_PATH` and pinned commit before
reading agent logs.

### 3. Record the actual container image reference and digest

```bash
CONTAINER=team1_dev
docker inspect "$CONTAINER" \
  --format 'config={{.Config.Image}} image_id={{.Image}}'
docker image inspect "$(docker inspect "$CONTAINER" --format '{{.Image}}')" \
  --format 'Id={{.Id}} RepoDigests={{json .RepoDigests}}'
```

Expected: `Config.Image` is the intended immutable registry digest and `.Image`
matches its local image ID. A tag-only or unexpected digest is an image rollout
problem, not a DSH runtime problem.

### 4. Verify DSH inside the running image

```bash
docker exec "$CONTAINER" dsh --version
```

Expected: `0.1.0-rc.6`. `dsh: not found` means the image was built before the
Dockerfile bake or the wrong image was selected. A different version means the
Dockerfile pin and `agents/deepseek.py::DSH_VERSION` are out of sync.

### 5. Check the host-side readiness contract and agent install

The runner must construct a DeepSeek `AgentConfig` with non-empty `base_url`,
`mcp_endpoint`, and `team_token`. These deployment-owned runtime inputs are
required for the MCP readiness handshake; the adapter rejects a missing value
before it writes container state. `dsh --dump-config` alone is not readiness.
Verify only presence in redacted config output, never the values:

```bash
python - <<'PY'
from models.agent import AgentConfig
from agents.deepseek import DeepSeekAgentManager
cfg = AgentConfig(provider="deepseek", cli="dsh", model="deepseek-v4-flash")
try:
    DeepSeekAgentManager._validate_readiness_contract(cfg)
except ValueError as exc:
    print(type(exc).__name__, str(exc))
PY
# expected for this deliberately incomplete probe: all three field names
```

### 6. Check agent install and `verify_cli`

```bash
rg -n 'agent installed|verify_cli|dsh|stale|not found' "$JOB_LOG"
jq -r 'select(.type == "AGENT_INSTALLED") | [.team_id,.type] | @tsv' events.jsonl
```

Expected: no `verify_cli` failure and one `AGENT_INSTALLED` for the DSH team.
Failure before `AGENT_INSTALLED` stays in image/provisioning; proceed only after
the pinned binary passes.

### 7. Check DeepSeek routing without printing secrets

The adapter writes `DEEPSEEK_API_KEY` and `DEEPSEEK_BASE_URL` into the agent
environment. The API router separately needs its deployment-owned upstream key
(commonly `API_ROUTER_KEY`) and a route matching the submitted model. The DSH
adapter route is `deepseek-official`; the wire model is the YAML model unchanged.

```bash
docker exec "$CONTAINER" sh -lc '
  test -n "$DEEPSEEK_API_KEY" && echo DEEPSEEK_API_KEY=present || echo DEEPSEEK_API_KEY=missing
  test -n "$DEEPSEEK_BASE_URL" && echo DEEPSEEK_BASE_URL=present || echo DEEPSEEK_BASE_URL=missing
'
docker exec api-router sh -lc '
  test -n "$API_ROUTER_KEY" && echo API_ROUTER_KEY=present || echo API_ROUTER_KEY=missing
'
jq '{routes,upstreams,model_limits}' run/apirouter-config.yaml
```

Expected: all required variables are present, the agent base URL points at the
run's API router, and `deepseek-v4-flash` resolves to the intended upstream.
Never print the variable values. A missing key or unmatched route is a deploy
injection/configuration failure.

### 8. Check network, proxy, and runtime installation

```bash
docker exec "$CONTAINER" sh -lc '
  for n in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
    eval "test -n \"\${$n:-}\"" && echo "$n=present" || echo "$n=missing"
  done
  getent hosts api-router mcp-gateway
'
rg -n 'dev-setup.sh|configured_proxy_http_403|timeout|npm|ENET|EAI_AGAIN' "$JOB_LOG"
```

Expected: all proxy variants are present, service names resolve, and
`dev-setup.sh` completes. Runtime `npm install` is not a recovery mechanism:
the isolated run must fail closed if the required baked CLI is absent. Fix and
republish the image instead of widening egress or installing a floating package.

### 9. Check MCP startup, `AGENT_READY`, and the dry run

```bash
jq -r '
  select(.team_id == "team1" and
    (.type == "DRY_RUN_START" or .type == "AGENT_READY" or
     .type == "DRY_RUN_OK" or .type == "DRY_RUN_FAILED")) |
  [.ts,.type,.payload.error_code // ""] | @tsv
' events.jsonl
```

Expected order: `DRY_RUN_START`, MCP startup, `AGENT_READY`, `DRY_RUN_OK`.
The profile warmup is offline and does not prove this sequence. If readiness is
missing, inspect closed `attempt_diagnostics`, the redacted DSH stderr class,
and MCP startup events. Do not infer readiness from process existence or a
clean profile exit. Fix MCP config/tool execution before a costly match.

### 10. Check the DSH session JSONL and emitter/parser

```bash
docker exec "$CONTAINER" sh -lc '
  find "$DSH_HOME/sessions" -name session.jsonl -type f -size +0c -print
'
find run/artifacts -name 'stream-async-*.jsonl' -type f -printf '%p %s bytes\n'
jq -s '
  {assistant: map(select(.type=="dsh" and .role=="assistant"))|length,
   usage: map(select(.type=="dsh" and .role=="usage"))|length,
   tool_results: map(select(.type=="dsh" and .role=="tool"))|length,
   tool_calls: map(select(.type=="dsh" and .role=="assistant") |
                   (.tool_calls // []) | length) | add}
' run/artifacts/team1/stream-async-0.jsonl
```

Expected: plaintext `session.jsonl`, a non-empty arena stream, valid `type=dsh`
rows, and matching tool-call/tool-result counts for completed calls. A populated
session with an empty arena stream isolates the failure to emitter launch,
tailing, or parsing rather than the model.

### 11. Check normal exit and forced final drain

```bash
jq . run/artifacts/team1/metadata-async-0.json
tail -n 5 run/artifacts/team1/stream-async-0.jsonl \
  | jq -c '{type,role,name,has_text:has("text"),has_usage:has("input_tokens")}'
jq -r 'select(.team_id=="team1" and
  (.type=="AGENT_COMPLETED" or .type=="AGENT_STOPPED" or
   .type=="AGENT_TIMEOUT" or .type=="AGENT_FAILED")) |
  [.ts,.type,.payload.exit_code // "",.payload.reason // ""] | @tsv' events.jsonl
```

Expected: events emitted before normal exit, SIGTERM, exit 137, or timeout are
still present after the terminal lifecycle event. `exit_code: 137` is normal for
an async process killed at match end; it is not evidence of an empty trajectory.
Compare the last session sequence with the last emitted row when diagnosing a
missing tail.

### 12. Reconcile artifact, events, and agent log

```bash
find run/artifacts/team1 -maxdepth 1 -type f -printf '%f %s bytes\n' | sort
jq -e . run/artifacts/team1/stream-async-0.jsonl >/dev/null
jq -e . run/events.jsonl >/dev/null
rg -n 'AGENT_READY|DRY_RUN_OK|AGENT_PROMPT_SENT|AGENT_STOPPED' run/events.jsonl
```

Expected: non-empty stream and agent log, valid JSONL, metadata for the same
chunk, and lifecycle events spanning readiness through stop. Only after these
local contracts pass should the provider API status, account authorization,
rate limits, or quota be investigated.

### 13. Check provider API and quota last

Use the API router's redacted request IDs, status classes, model, upstream ID,
and usage counters. Do not replay keys with ad hoc curl commands or copy response
bodies into an issue. A 401/403/429/5xx here is actionable only after steps 1-11
prove the intended code, image, CLI, injection, network, readiness, and stream.

## Live evidence and limits

### Reproducible clean-checkout contract

After merging, the runner must use the merged orche checkout as host-side code
and a newly built immutable image. Deploy must pass non-empty `base_url`,
`mcp_endpoint`, and `team_token` into the DeepSeek `AgentConfig`; an old worker
process or a tag-only image is not equivalent.

```bash
git clone <orche-url> orche
cd orche
git checkout <merge-sha>
# The dsh harness image is layered on the shared agent base: build the base at
# the same sha, then the deepseek harness FROM it.
docker build --no-cache --pull -t cyberarena/agent-base dockerfiles/agent/base
docker build --no-cache --build-arg BASE_IMAGE=cyberarena/agent-base \
  -t <registry>/cyberarena/agent-deepseek:dsh-<merge-sha> dockerfiles/agent/deepseek
docker run --rm --entrypoint dsh \
  <registry>/cyberarena/agent-deepseek:dsh-<merge-sha> --version
docker push <registry>/cyberarena/agent-deepseek:dsh-<merge-sha>
docker image inspect <registry>/cyberarena/agent-deepseek:dsh-<merge-sha> \
  --format 'Id={{.Id}} RepoDigests={{json .RepoDigests}}'
```

Use the resulting `registry/name@sha256:<digest>` in every submitted dev node.
`scripts/build_images.sh` only builds local/runner images; merging this PR does
not build, push, or refresh an existing worker image. Record
`CAD_ORCHE_PATH`, `CAD_ORCHE_PINNED_COMMIT`, image digest, and readiness-field
presence before submission. Start with one minimal full lifecycle, then scale.

For clean exit, SIGTERM, exit 137, or timeout, the emitter drains received dsh
session JSONL before the phase group is reaped. Verify artifacts and closed
events after terminal state; a clean dsh exit is not success if `AGENT_READY`
or the stream is absent.

Job `77dc9d38f4c6` validated the minimum real lifecycle on 2026-08-21:

- deploy commit `1b65733e9a59b51f9f514583bc453285f04c44c0`
- orche commit `5040fdc682cda0e5200ef1181d0233ba9bca9e84`
- warehouse commit `afdbcaac05ad4c9fde10041356c421935af1e5a4`
- image `cyberarena/agent:deepseek-fix-20260821`
- image ID/digest `sha256:1282473e10411ce2a08aeda82dd6a15c5aa2fadbf01791696002566351c31fb3`
- one 45-second async round; DeepSeek DSH versus Codex
- job `succeeded`, match exit code 0; DeepSeek chunk exit code 137 at match stop
- artifact SHA-256 `f1791265a0d3697ffd4108041872fa7914e8bdc8dd9e256a9406eb3a2bd8f739`
- DeepSeek stream 21,852 bytes/18 rows: 5 assistant, 5 usage, 8 tool calls,
  and 8 corresponding tool results
- `events.jsonl`: `AGENT_READY`, `DRY_RUN_OK`, `AGENT_PROMPT_SENT`, and
  `AGENT_STOPPED` for the DeepSeek team

This was a minimum real match, not the multi-round/full-duration acceptance
batch. It directly validates a tool-using exit-137 lifecycle and artifact
capture. Normal-exit, explicit SIGTERM, timeout, stale-session isolation, and
non-DeepSeek compatibility are covered by automated tests; each remains a
candidate for a future live fault-injection run before claiming live coverage
of every termination mode.
