# How to run the sample challenge

Recipe for taking a blank Linux host (typically an EC2) to a finished
match of the bundled DEF CON ico challenge, using the **v3 isolated
topology**: agents sit on `internal:true` networks reaching the LLM only
through the api-router egress proxy and MCP only through the on-net
mcp-gateway; the flag-service + health-poller run as off-agent containers
that plant/probe prod over SSH; GameMgr runs on the host. Same steps work
for your own challenges — just swap the config file. Tested most recently
on Amazon Linux 2023; should work on any modern distro.

You need a Linux host (**x86_64** — the facility images pin amd64; ≥ 8 GB
disk), sudo, and read access to this repo. The bundled example config
(`config/game_ico_v3_isolated_async_mixed.yaml`) runs **team1 on Claude,
team2 on OpenAI Codex**, so you need one Anthropic key + one Codex key (or
edit the config to use two Anthropic keys). Run everything below on the box.

## 1. Install docker + python 3.10+

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

`mcp` needs python 3.10+, and AL2023 / Ubuntu 22.04 ship 3.9 / 3.10 by
default — installing 3.11 explicitly avoids surprises.

## 2. Add your SSH key to GitHub

The repo is private. Generate a key, paste the public half at
https://github.com/settings/keys:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub
```

## 3. Clone, install, sync vendor

```bash
git clone git@github.com:cyber-arena-ai/octagon.git
cd octagon
python3.11 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip && pip install -r requirements.txt
bash scripts/sync_nautilus.sh     # pins nautilus-institute/finals-2025 @ 366520c0cc43
```

## 4. Create `.env`

```bash
cat > .env <<'EOF'
export ANTHROPIC_API_KEY_TEAM1=sk-ant-...           # team1 (Claude)
export CODEX_API_KEY_TEAM2=sk-...                   # team2 (Codex); or another ANTHROPIC_API_KEY_TEAM2
export CYBERARENA_FACILITY_TOKEN=$(openssl rand -hex 16)   # facility<->GameMgr auth (any random string)
# export CYBERARENA_ADMIN_TOKEN=$(openssl rand -hex 16)    # OPTIONAL: gates POST /game/start,/game/stop (X-Admin-Token). Blank => disabled (403); stop via SIGINT.
EOF
chmod 600 .env
```

`main.py` auto-loads `.env` via `python-dotenv`. The agents reach the LLM
through the api-router (`base_url: http://api-router:8080` in the config), which
forwards verbatim to the upstream the firewall allows — so the keys are the real
upstream keys. `CYBERARENA_FACILITY_TOKEN` authenticates the flag-service /
health-poller callbacks into GameMgr; it's never written to the repo or events.

## 5. Build the infra images (once)

`main.py start` builds the **challenge** image (`cyberarena/chal-nautilus-ico`,
tag pinned by `sync_nautilus.sh`) and the **per-game facility** images
(`cyberarena/flag-service`, `cyberarena/health-poller`) on its own. The **infra +
base** images are one-time prerequisites — `start` runs containers from them but
doesn't build them. One script builds the whole set (idempotent; layer-cached):

```bash
# Supply the apirouter firewall config first, so the script can bake api-router-cfg:
mkdir -p apirouter-data && cp apirouter/config.example.yaml apirouter-data/config.yaml
$EDITOR apirouter-data/config.yaml          # set the egress allow-list

bash scripts/build_images.sh
# builds: agent-base + agent-<harness> for every harness under dockerfiles/agent/ ·
#         mcp-gateway · flag-service-base · health-poller-base ·
#         api-router (+ api-router-cfg if apirouter-data/config.yaml exists)
# The sample config runs claude vs codex, so this is enough and much faster:
#   AGENT_HARNESSES="claude codex" bash scripts/build_images.sh
```

`apirouter-data/` is gitignored (it holds your deployment-specific firewall
config). If you skip it, the script builds everything except `api-router-cfg` and
prints how to add it. See [DEPLOY_ISOLATED_AGENT.md](DEPLOY_ISOLATED_AGENT.md) for
the apirouter config / isolation model.

## 6. Run

No `setup` step — `start` is self-contained (builds challenge + facility images,
creates the networks/containers, plants flags, runs the match):

```bash
nohup python main.py start -c config/game_ico_v3_isolated_async_mixed.yaml > /tmp/game.log 2>&1 &
tail -f /tmp/game.log
```

Backgrounding with `nohup` is for SSH sessions; on the box directly just run it in
the foreground. The bundled config is async, 5 rounds × 5 min (~25 min).

If you skipped step 5, `start` stops immediately with `Required base images not
found: … Please run bash scripts/build_images.sh first` — build them and re-run.

The live panel is at `http://localhost:8000/panel`. To view it from
your laptop, forward the port:

```bash
ssh -L 8000:localhost:8000 <user>@<host>          # run this on your laptop
```

When you're done, tear everything down:

```bash
python main.py teardown -c config/game_ico_v3_isolated_async_mixed.yaml
```

## 7. Verify

```bash
latest=$(ls -1t runs/ | head -1)
jq -r '.type' runs/$latest/events.jsonl | sort | uniq -c | sort -rn
jq -c 'select(.type=="GAME_END")' runs/$latest/events.jsonl
jq -c 'select(.type=="FLAG_SUBMITTED" and .payload.accepted==true)' runs/$latest/events.jsonl
```

Healthy run (bundled 5-round config):

- `GAME_END` with `reason=completed`, `final_round=5`
- ≥ 1 accepted flag submission
- `FLAG_PLANTED` each round, 100+ × `SERVICE_HEALTH_CHECK` all levels passing
- `SERVICE_RESTART_*` and `MCP_ATTACKS_QUERY` counts vary (agent-driven in async);
  `SERVICE_RESTART_FAILED` should be 0

Copy `runs/<ts>/` off the box if you want the `events.jsonl` and
`panel.html` after the instance is terminated.
