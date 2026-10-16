# How to run the sample challenge

Recipe for taking a blank host to a finished match of the bundled DEF CON
`nautilus-ico` challenge, using the **v3 isolated topology**: agents sit on
`internal:true` networks reaching the LLM only through the api-router egress
proxy and MCP only through the on-net mcp-gateway; the flag-service +
health-poller run as off-agent containers that plant/probe prod over SSH;
GameMgr runs on the host. The same steps work for any challenge — just swap the
config file. Tested most recently on Amazon Linux 2023.

**Host:** Docker + Python 3.10+, sudo, ≥ 8 GB disk. **x86_64 Linux is the tested
path** — the facility / mcp-gateway / health-poller images pin `linux/amd64`; on
Apple Silicon (arm64) they build and run under QEMU emulation (slower) and
bring-up works, but run a full match on an x86_64 host.

**Keys:** the bundled config (`config/game_ico_v3_isolated_async_mixed.yaml`)
runs **team1 on Claude, team2 on Codex (GPT)**, but both teams reach the LLM
through the **api-router**, so you supply just **one** upstream key
(`API_ROUTER_KEY`) for a provider that serves both model families (e.g. the
default reseller `app-us.ppapi.ai`). To bill Claude and GPT to native providers
instead, see [ADD_LLM_UPSTREAM.md](ADD_LLM_UPSTREAM.md).

## 1. Install Docker + Python 3.10+

```bash
# Amazon Linux 2023
sudo dnf install -y docker git python3.11 python3.11-pip python3.11-devel

# Debian / Ubuntu
sudo apt-get install -y docker.io git python3.11 python3.11-venv python3-pip
```

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
exit          # re-login so the docker group takes effect
```

`mcp` needs Python 3.10+, and AL2023 / Ubuntu 22.04 ship 3.9 / 3.10 by default —
installing 3.11 explicitly avoids surprises. (macOS: Docker Desktop or OrbStack
running, plus any recent Python.)

## 2. Clone + install

Octagon is public — a plain clone, no SSH key needed. Challenges live in the
`challenge-warehouse` submodule, and the sample challenge's vendored source is
committed there, so nothing is fetched out-of-band:

```bash
git clone --recurse-submodules https://github.com/cyber-arena-ai/octagon.git
cd octagon
# (cloned without --recurse-submodules? run: git submodule update --init)

python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
```

> `scripts/sync_nautilus.sh` is **not** needed for a fresh run — it only re-pins
> the vendored nautilus source when upgrading. `scripts/update_challenges.sh`
> bumps the whole warehouse to its latest main.

## 3. Create `.env`

```bash
cat > .env <<'EOF'
API_ROUTER_KEY=sk-...                                 # the ONE real upstream key the api-router forwards with
CYBERARENA_FACILITY_TOKEN=$(openssl rand -hex 16)     # facility<->GameMgr auth (any random string)
# CYBERARENA_ADMIN_TOKEN=$(openssl rand -hex 16)      # OPTIONAL: gates POST /game/start,/game/stop (X-Admin-Token). Blank => disabled (403); stop via SIGINT.
EOF
chmod 600 .env
```

`main.py` auto-loads `.env` via `python-dotenv`. Both teams reach the LLM through
the api-router (`base_url: http://api-router:8080` in the config); each team's
`api_key` is a **non-secret principal token** (`cad-team1` / `cad-team2`, declared
plainly in the config), and the router substitutes the real `API_ROUTER_KEY` on
forward — so an agent never holds a provider key. `CYBERARENA_FACILITY_TOKEN`
authenticates the flag-service / health-poller callbacks into GameMgr; it's never
written to the repo or events, and a v3 config **won't load** if it's unset.

## 4. Build the infra images (once)

`main.py start` builds the **challenge** image (`cyberarena/chal-nautilus-ico`,
tag pinned by the challenge) and the **per-game facility** images
(`cyberarena/flag-service`, `cyberarena/health-poller`) on its own. The **infra +
base** images are one-time prerequisites — `start` runs containers from them but
doesn't build them. One script builds the whole set (idempotent; layer-cached):

```bash
# Supply the apirouter firewall config first, so the script can bake api-router-cfg:
mkdir -p apirouter-data && cp apirouter/config.example.yaml apirouter-data/config.yaml
$EDITOR apirouter-data/config.yaml          # set the egress allow-list

# The sample config runs claude vs codex, so scope the build to those two (much faster):
AGENT_HARNESSES="claude codex" bash scripts/build_images.sh
# builds: agent-base + agent-claude + agent-codex · mcp-gateway ·
#         flag-service-base · health-poller-base · api-router (+ api-router-cfg)
# Omit AGENT_HARNESSES to build every harness under dockerfiles/agent/.
```

`apirouter-data/` is gitignored (it holds your deployment-specific firewall
config). If you skip it, the script builds everything except `api-router-cfg` and
prints how to add it. See [DEPLOY_ISOLATED_AGENT.md](DEPLOY_ISOLATED_AGENT.md) for
the apirouter config / isolation model.

## 5. Run

No `setup` step — `start` is self-contained (builds the challenge + facility
images, creates the networks/containers, plants flags, runs the match):

```bash
nohup python main.py start -c config/game_ico_v3_isolated_async_mixed.yaml > /tmp/game.log 2>&1 &
tail -f /tmp/game.log
```

Backgrounding with `nohup` is for SSH sessions; on the box directly just run it in
the foreground. The bundled config is async, 5 rounds × 5 min (~25 min).

> **Quick smoke test (~5 min).** To validate the pipeline without a full match,
> copy the config and shrink the round loop — in `round_config` set
> `total_rounds: 1` and a shorter `round_secs` (e.g. 180) — then `start` that
> copy. One round still exercises build → bring-up → flag plant → agents →
> health checks → teardown.

If you skipped step 4, `start` stops immediately with `Required base images not
found: … Please run bash scripts/build_images.sh first` — build them and re-run.

The live panel is at `http://localhost:8000/panel`. To view it from your laptop,
forward the port:

```bash
ssh -L 8000:localhost:8000 <user>@<host>          # run this on your laptop
```

When you're done, tear everything down:

```bash
python main.py teardown -c config/game_ico_v3_isolated_async_mixed.yaml
```

## 6. Verify

```bash
latest=$(ls -1t runs/ | head -1)
jq -r '.type' runs/$latest/events.jsonl | sort | uniq -c | sort -rn
jq -c 'select(.type=="GAME_END")' runs/$latest/events.jsonl
jq -c 'select(.type=="FLAG_SUBMITTED" and .payload.accepted==true)' runs/$latest/events.jsonl
```

Healthy run (bundled 5-round config; for the smoke test above, `final_round=1`):

- `GAME_END` with `reason=completed`, `final_round=5`
- ≥ 1 accepted flag submission
- `FLAG_PLANTED` each round, 100+ × `SERVICE_HEALTH_CHECK` all levels passing
- `SERVICE_RESTART_*` and `MCP_ATTACKS_QUERY` counts vary (agent-driven in async);
  `SERVICE_RESTART_FAILED` should be 0

Copy `runs/<ts>/` off the box if you want the `events.jsonl` and `panel.html`
after the instance is terminated.
