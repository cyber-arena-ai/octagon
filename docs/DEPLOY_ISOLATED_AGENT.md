# Deploying the isolated agent (apirouter as the sole egress)

End-to-end recipe for running an agent that **cannot reach the public internet
except through apirouter**. Combines three pieces shipped in this repo:

- [dockerfiles/agent/](../dockerfiles/agent/) — agent image (Node 24 LTS, Claude
  Code baked, pentest tooling, proxy-aware `entrypoint.sh`).
- [apirouter/](../apirouter/) — Anthropic/OpenAI-format passthrough that meters
  per-principal, fixes third-party quirks (markdown fences, `reasoning`/`reasoning_effort`,
  `max_tokens` overruns), and runs a forward proxy with an egress allow list.
- The new `Network.internal` field on the orchestrator network model — makes
  the agent's network a Docker `--internal` bridge (no NAT, no default
  gateway), so direct egress is impossible at L3.

Use this when you need a hard guarantee that nothing the agent does — Claude
Code calls, `npm install`, `apt-get`, exploits, `curl` from generated code —
can reach the public internet without crossing apirouter's allow list.

For the simple "agent talks directly to ppapi" setup, you don't need any of
this; see [RUN_SAMPLE_CHALLENGE.md](RUN_SAMPLE_CHALLENGE.md) and the regular
[.env.example](../.env.example) instead.

## Architecture

```
┌─────────────── agent network (internal: true) ───────────────┐
│                                                                │
│   ┌──────────────┐         ┌─────────────────────────┐         │
│   │  agent       │ ──────▶ │  api-router             │         │
│   │  container   │         │  :8080  metering GW     │         │
│   │              │         │  :3129  forward proxy   │         │
│   │  HTTPS_PROXY │         │  (firewall: allow-list) │         │
│   │  env         │         └─────────────────────────┘         │
│   └──────────────┘                       │                     │
│         no internet route                │ joins egress_net    │
└──────────────────────────────────────────┼─────────────────────┘
                                           ▼
                                  ┌─────────────────┐
                                  │  egress_net     │
                                  │  (NAT'd bridge) │
                                  └─────────────────┘
                                           │
                                           ▼
                                       internet
```

Two Docker networks, with apirouter being the only thing on **both**:

- **`team_net`** — the agent's network, `internal: true`. No default gateway,
  no NAT; DNS doesn't resolve external names; direct egress is impossible at
  the kernel level.
- **`egress_net`** — a normal bridge with NAT to the host. Only apirouter sits
  on it; the agent never does.

## Per-tool proxy behavior (read this once)

The agent's traffic to the LLM upstream uses **`ANTHROPIC_BASE_URL=api-router:8080`** —
that's a direct same-network connection, *not* via the forward proxy. The
forward proxy (`:3129`) handles every *other* outbound:

| Tool                  | Honors `HTTPS_PROXY` env? | Notes |
|-----------------------|---------------------------|-------|
| `curl` / `wget`       | ✓ native (lowercase env)  | — |
| `pip`                 | ✓ native                  | — |
| `npm`                 | ✓ native                  | (No runtime CLI install any more — each harness is baked into its image.) |
| `git`                 | ✓ native                  | — |
| `apt`                 | ✗ **does NOT honor env**  | Worked around: the agent base image's `entrypoint.sh` translates `HTTPS_PROXY` → `/etc/apt/apt.conf.d/99proxy-from-env` at container start. Just set the env var; apt then works for `_run_install_list`. |
| Node `fetch` (undici) | ✗ unless `NODE_USE_ENV_PROXY=1` | The image bakes this ENV. Claude Code talks to apirouter directly on `:8080`, but any ad-hoc node scripts the agent writes will now route correctly. |

## game.yaml — minimum wiring

```yaml
networks:
  team1_net:
    subnet: 10.10.1.0/24
    internal: true              # ← Docker --internal: NO NAT, NO external route
  egress_net:
    subnet: 10.10.99.0/24       # normal bridge; only api-router sits here

nodes:
  - id: api-router               # one shared apirouter; both teams point at it
    image: cyberarena/api-router
    networks: [team1_net, team2_net, egress_net]
    ports: []                    # nothing exposed to host; reachable on team_nets
    env:
      UPSTREAM_API_KEY: ${UPSTREAM_API_KEY}   # the ONE real upstream key
      APIROUTER_CONFIG: /data/config.yaml

  - id: team1_dev                # the agent container
    image: cyberarena/agent-claude   # per-harness: cyberarena/agent-<agent.provider>,
                                     # built from dockerfiles/agent/<provider>/ on
                                     # top of dockerfiles/agent/base/
    networks: [team1_net]        # ← internal-only, no egress_net
    env:
      # apirouter forward proxy. lowercase + uppercase covers every tool.
      HTTPS_PROXY: "http://api-router:3129"
      HTTP_PROXY:  "http://api-router:3129"
      https_proxy: "http://api-router:3129"
      http_proxy:  "http://api-router:3129"
      NO_PROXY:    "127.0.0.1,localhost,api-router,host.docker.internal"
      no_proxy:    "127.0.0.1,localhost,api-router,host.docker.internal"

  - id: team1_prod
    image: cyberarena/chal-...
    networks: [team1_net]
    extra_hosts:
      host.docker.internal: host-gateway

teams:
  - id: team1
    dev: team1_dev
    prod: team1_prod
    token: ${TEAM1_TOKEN}
    agent:
      cli: claude -p --dangerously-skip-permissions --output-format stream-json --verbose
      model: claude-haiku-4-5-20251001    # any model ppapi supports (claude/gpt/gemini)
      base_url: http://api-router:8080    # apirouter's metering gateway
      api_key:  ${TEAM1_APIROUTER_TOKEN}  # this team's principal token (NOT the upstream key)
```

The agent never holds the real upstream key. It authenticates to apirouter
with its **principal token** (per-team, declared in `apirouter-data/config.yaml`'s
`principals:` block); apirouter injects the real upstream key on forward.

## apirouter config (the firewall lives here)

[apirouter/config.example.yaml](../apirouter/config.example.yaml) → copy to
`apirouter-data/config.yaml`, fill `${UPSTREAM_API_KEY}` from env. The two
load-bearing sections for this deployment:

```yaml
compat:
  # Third-party "OpenAI-compatible" upstream quirks. All opt-in.
  drop_empty_content: true            # #1
  drop_params: ["reasoning_effort", "reasoning"]   # #3 (ppapi rejects 'reasoning')
  json_schema_fallback: true          # #4
  retry_on_5xx: 2                     # #10
  retry_backoff_ms: 500
  max_request_bytes: 5242880          # #5/#6
  clamp_max_tokens:                   # #11 — Claude Code defaults to 64k; some
    gpt-4o-mini: 16384                #       OpenAI models cap lower. Per-model.
    gpt-4o: 16384

firewall:
  default: deny                       # implicit deny for everything not listed
  allow:
    domains:
      - "*.ppapi.ai"                  # the LLM upstream
      - "registry.npmjs.org"          # npm i (claude-code refresh at install time)
      - "api.anthropic.com"           # if you bypass ppapi for some teams
      # add per-deployment as needed:
      # - "*.ubuntu.com"              # if install_list does apt-get install
      # - "pypi.org"; - "files.pythonhosted.org"   # if anything pip installs
      # - "github.com"                # if anything git clones at runtime
    cidrs: []
```

## Build & verify

```bash
# 1. Build the agent images: the shared base, then the harness(es) your config uses.
docker build -t cyberarena/agent-base   dockerfiles/agent/base
docker build -t cyberarena/agent-claude dockerfiles/agent/claude
# (or all of them: bash scripts/build_images.sh)

# 2. Build apirouter image (unchanged; already wired).
docker build -t cyberarena/api-router apirouter

# 3. Run the orchestrator as usual — it picks up `internal: true` automatically.
.venv/bin/python main.py start --config config/your-game.yaml

# 4. Sanity: from inside any agent container, direct egress must fail.
docker exec team1_dev curl -m 4 -s -o /dev/null -w "HTTP %{http_code}\n" https://1.1.1.1/
# Expected: HTTP 000 (no route — proven blocked at L3)

# 5. Through the proxy, allow-listed hosts reach; non-allowed get 403.
docker exec team1_dev bash -lc 'curl -m 8 -s -o /dev/null -w "HTTP %{http_code}\n" https://app-us.ppapi.ai/'
# Expected: HTTP 307 / 200 (allowed)
docker exec team1_dev bash -lc 'curl -m 8 -s -o /dev/null -w "HTTP %{http_code} via proxy\n" https://github.com/'
# Expected: HTTP 000 — curl exits 56 with "Received HTTP code 403 from proxy after CONNECT"

# 6. Inspect what apirouter recorded:
curl -s http://<api-router-host>:8080/usage              # per-principal totals
tail apirouter-data/usage.jsonl                          # per-call rows incl. egress denies
```

## Troubleshooting

- **`apt-get install ...` hangs or "Could not resolve":** the entrypoint didn't
  see `HTTPS_PROXY` at container start. Check `docker inspect team1_dev | grep
  -i proxy` and `docker exec team1_dev cat /etc/apt/apt.conf.d/99proxy-from-env`.
- **`npm install` fails with `EAI_AGAIN`:** `registry.npmjs.org` not in the
  apirouter allow list, or the agent's `HTTPS_PROXY` is unset.
- **gpt-4o-mini etc. 400 `max_tokens is too large: 64000`:** the model isn't
  in apirouter's `clamp_max_tokens` table. Add it.
- **OpenAI-only upstream (not ppapi):** Claude Code speaks Anthropic Messages
  and a raw OpenAI endpoint will 404 on `/v1/messages`. Either switch to a
  multi-format upstream (ppapi / litellm proxy), or re-add a translator at
  the agent boundary — see the "No translator baked in" note in
  [docs/UML/agent_manager.md](UML/agent_manager.md).
- **Agent can still reach the internet directly:** the network wasn't created
  with `internal: true`. Confirm with `docker network inspect team1_net | grep
  Internal` (must show `"Internal": true`). Existing networks are NOT
  retroactively converted — `docker network rm team1_net` + restart the
  orchestrator.
