# dsh trajectory stream — emitter design + emitted schema

The DeepSeek Harness (`dsh`, provider `deepseek`) adapter
([agents/deepseek.py](../agents/deepseek.py)) emits a **populated per-tool-call
trajectory** for headless runs. This doc is the emitter side of the
emitter⇄parser contract for that stream; the parser side lives in
[gamemgr/transcripts.py](../gamemgr/transcripts.py) (`_is_dsh_line` /
`_unpack_dsh_line`, Board #954). The shared contract is also captured as
Artifact #71.

## The problem it fixes

`dsh --profile headless` runs a task to quiescence and prints **only the final
assistant text** on stdout, buffered to phase end (its runner `summarize()`s the
session and writes `outcome.text`). The arena tees that stdout into
`artifacts/<team>/stream-async-<n>.jsonl`, so a headless run produced a single
end-of-phase blob with **no per-tool-call structure** — and because the async
supervisor SIGKILLs the long-lived phase at round/match end, that buffered final
write was truncated and the file landed **0 bytes** (observed: dsh team1
`stream-async-0.jsonl` = 0 bytes). An unrecognized/empty stream also renders
blank on the frontend (`collect_transcripts` → `[]`).

## The mechanism

dsh's `session-persistence-jsonl` backend already writes **every** session event
(`{type, seq, time, data}`) to an append-only `session.jsonl`, incrementally as
the run proceeds — durable across a SIGKILL because it is flushed as it grows,
not buffered to phase end. That log is the real trajectory.

- The arena overlay patch (`_build_overlay_patch`) overrides the
  `session-persistence-jsonl` row to write **plaintext** (`compression: none`,
  base default is `zstd`) under a known root (`$DSH_HOME/sessions`).
- `phase_command` no longer tees dsh's own stdout. It runs
  [agents/dsh_trajectory_emitter.py](../agents/dsh_trajectory_emitter.py)
  alongside dsh: the emitter tails that `session.jsonl`, translates each event to
  the schema below, and prints it to stdout — which `run_phase` tees, live, into
  `stream-async-<n>.jsonl`. dsh's own stdout/stderr go to files under
  `$DSH_HOME` (kept for debugging). Because lines stream as events occur, a
  mid-phase SIGKILL keeps everything already emitted. On normal dsh exit the
  emitter is SIGTERM'd and drains the final events before exiting.

## Emitted schema (one JSON object per line)

Every line carries a discriminating top-level **`"type": "dsh"`** (no other
harness schema sets this, so detection cannot regress Claude/codex/opencode/kimi),
a `ts` (Unix epoch **seconds**, float — dsh's event `time` in ms ÷ 1000), and a
`role`.

**assistant** — a model step; carries any of thought / text / tool_calls, each
rendered as its own card:

```json
{"type":"dsh","ts":1786000001.0,"role":"assistant",
 "thought":"reasoning text (optional)",
 "text":"visible assistant text (optional)",
 "tool_calls":[{"id":"call_1","function":{"name":"bash","arguments":"{\"command\":\"ls /srv\"}"}}]}
```

- `tool_calls` use the **OpenAI-nested function shape**
  (`{"id","function":{"name","arguments"}}`); `arguments` is the raw JSON string
  exactly as the model produced it (dsh's native `llm-deepseek` shape).
- An assistant step with none of thought/text/tool_calls emits no line.

**tool** — one completed tool result:

```json
{"type":"dsh","ts":1786000002.0,"role":"tool","tool_call_id":"call_1",
 "name":"bash","content":"app.py\nflag.txt\n","is_error":false}
```

- `name` is resolved from the matching tool call (dsh's `tool/result` event does
  not repeat it), keyed by `tool_call_id`.
- `content` is the tool output flattened to a string; `is_error` is the result
  block's `isError` OR a present `data.error`.

**usage** — optional per-step token accounting (→ a system card):

```json
{"type":"dsh","ts":1786000001.0,"role":"usage",
 "input_tokens":1200,"output_tokens":340,"cached_input_tokens":900}
```

## Sample + validation

A captured non-empty sample is committed at
[docs/samples/dsh-stream-async-sample.jsonl](samples/dsh-stream-async-sample.jsonl)
— **real emitter output**, produced by running the translator over a
representative dsh headless session-event log (see
`tests/test_dsh_trajectory_emitter.py::REPRESENTATIVE_SESSION_EVENTS`).

Validated:

- `tests/test_dsh_trajectory_emitter.py` pins the translation field-by-field and
  the live-tail path (incremental append + SIGTERM drain) end-to-end.
- The sample feeds the #954 parser (`_unpack_dsh_line`) into **9 non-empty
  per-step cards** (thinking · text · tool_use×2 · tool_result×2 · usage×2 · final
  text), `summary_stats` = `2 tools (1×bash, 1×read_file)`, tokens 2700/400/900 —
  a zero-diff reconcile against the locked schema.

### Live validation

The emitter and parser were validated in a real one-round async match on
2026-08-21. The DeepSeek chunk was stopped by the normal async match teardown
(`exit_code: 137`) after 45 seconds. Its artifact retained all events received
before teardown:

- job: `77dc9d38f4c6` (`succeeded`, match exit code 0)
- orche: `5040fdc682cda0e5200ef1181d0233ba9bca9e84`
- warehouse: `afdbcaac05ad4c9fde10041356c421935af1e5a4`
- agent image: `cyberarena/agent:deepseek-fix-20260821`
- image digest: `sha256:1282473e10411ce2a08aeda82dd6a15c5aa2fadbf01791696002566351c31fb3`
- `stream-async-0.jsonl`: 21,852 bytes, 18 valid JSONL rows
- rows: 5 assistant, 5 usage, 8 tool results; the assistant rows contained 8
  tool calls, so every captured call had a captured result
- tools: 6 `bash`, 1 `mcp__cyberarena__get_game_state`, and 1
  `mcp__cyberarena__get_attack_targets`

The last assistant/usage pair and its two tool results preceded the recorded
`AGENT_STOPPED` event and remained in the artifact after exit 137. This proves
the live exit-137 path preserves events already received by the emitter. Unit
tests remain the coverage for deterministic SIGTERM final-drain timing and the
normal-exit/timeout variants; see the limitations in
[DSH_LIVE_RUNBOOK.md](DSH_LIVE_RUNBOOK.md#live-evidence-and-limits).
