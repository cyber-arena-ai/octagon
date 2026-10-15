# Adding an LLM upstream (api-router)

How to point the api-router at another LLM provider, or split traffic across
several. The router picks the upstream **per request from the model name the agent
sent**, injects *that* upstream's key, and meters per team. Full design:
[docs/UML/api_router.md](UML/api_router.md).

## TL;DR — two files, no rebuild

```bash
# 1. .env — the key, named API_ROUTER_KEY_<UPSTREAM_ID> (upper-cased; . and - become _)
API_ROUTER_KEY_DEEPSEEK=sk-...
```

```yaml
# 2. game.yaml — the api_router block
api_router:
  container: api-router
  upstreams:
    deepseek: { base_url: https://api.deepseek.com }   # no key field here — see below
  routes:
    default: deepseek                 # required once there is >1 upstream
    by_model:
      "deepseek-*": deepseek          # fnmatch globs, FIRST MATCH WINS
  firewall:
    allow:
      domains: ["api.deepseek.com"]   # so the agent's own curl can reach it (see note 3)
  pricing:                            # optional; omit = free (cost recorded as 0)
    deepseek-v4: { input: 0.27, output: 1.10 }
```

**Where's the key?** The upstream block has no key field on purpose — the key is
**required**, but its `.env` variable name is *derived from the upstream id*:
`deepseek` → `API_ROUTER_KEY_DEEPSEEK` (upper-cased). So you only put the value in
`.env` (step 1); you never write the var name in the yaml. `main.py` reads it from
`.env` and injects it into the router container at startup — you do **not** edit the
api-router node's `env:` and do **not** rebuild. To use a *different* variable name,
add `api_key_env:` (see below). `python main.py start -c <config>` regenerates and
remounts the router config each run.

## Backward compatibility

The single-upstream shorthand is unchanged and still the default:

```yaml
api_router:
  upstream_url: https://app-us.ppapi.ai     # key from node env UPSTREAM_API_KEY (<- .env API_ROUTER_KEY)
```

- Keep `upstream_url:` → nothing about your `.env` or node env changes.
- `upstream_url:` and `upstreams:`/`routes:` are **mutually exclusive** — declaring
  both is rejected at load (no precedence rule to remember). To go multi-upstream,
  delete `upstream_url:`.

## Reuse an existing env var instead of the convention

The `API_ROUTER_KEY_<ID>` name is only a default. To read a var you already have:

```yaml
upstreams:
  anthropic:
    base_url: https://api.anthropic.com
    api_key_env: ANTHROPIC_API_KEY_TEAM1   # bare NAME, not ${...}
```

It is a bare variable name on purpose: the game-config loader would expand `${...}`
and write the real key into the generated run artifact. The name is resolved to a
`${...}` placeholder that the router expands inside its own container, off disk.

## Notes / gotchas

1. **Every upstream needs a key.** There is no keyless mode — an agent holds a
   billing token, not a provider credential, so a keyless router would let it spend
   unmetered. A missing key aborts `start` with the exact `.env` line to add.
2. **`routes.default` is required with >1 upstream** — it's where a model matching no
   `by_model` glob (or a request with no model) goes. `by_model` is first-match-wins
   in declaration order, **not** most-specific-wins: put `"claude-haiku-*"` before
   `"claude-*"` if the narrow rule should bite.
3. **Firewall is for the agent, not the router.** The gateway's own upstream calls go
   out directly and never consult the firewall, so listing a host under
   `firewall.allow.domains` only grants the *agent's* curl/pip a path to it. Add it if
   the agent needs that host; a wrong firewall never breaks the LLM path.
4. **The router does not translate formats.** It forwards verbatim, so the client's
   request format must match what the upstream accepts. Claude Code speaks Anthropic
   Messages (`/v1/messages`) — route `claude-*` to an Anthropic-format upstream; a raw
   OpenAI-only endpoint 404s. Auth is covered for any provider using `Authorization:
   Bearer` or `x-api-key` (both are injected); an exotic auth header is not.
5. **Per-team budgets** live next to pricing and are enforced against computed cost, so
   a budget on an unpriced model can never fire (startup warns). See
   [docs/UML/api_router.md](UML/api_router.md).
