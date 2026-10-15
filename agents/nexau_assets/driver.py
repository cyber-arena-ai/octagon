"""NexAU SDK driver — one phase of a cyberarena agent, resumable across runs.

Invoked per phase by NexauAgentManager.phase_command as:

    python driver.py --agent <agent.yaml> --prompt-file <PROMPT_PATH> \
                     --session-id cyberarena --user-id cyberarena

Why not `nexau chat`: in NexAU v0.3.9 the `chat` command builds the agent with a
MINIMAL config path (only build_system_prompt_path), which SKIPS build_tools() and
build_mcp_servers() — so the agent gets neither its shell/file tools nor the MCP
server. The full build is `AgentConfig.from_yaml()` (used by `serve http/stdio`),
which this driver calls directly, then runs one query via `Agent.run()`. Model /
base_url / api_key come from env (LLM_MODEL / LLM_BASE_URL / LLM_API_KEY), which
NexAU's LLMConfig reads.

Resume: a fixed `--session-id` + a per-match SQLite db (under NEXAU_CLI_STATE_DIR)
means a fresh process each phase get-or-creates the same session, so later phases
resume the prior conversation.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.config.config import AgentConfig
from nexau.archs.session import SessionManager, SQLDatabaseEngine


def main() -> int:
    # Surface NexAU's internal logs (MCP connect, tool calls, errors) to a file so a
    # failed run is diagnosable — the arena doesn't tee the driver's stdout.
    logging.basicConfig(
        level=logging.INFO,
        filename=os.environ.get("NEXAU_DRIVER_LOG") or f"{os.environ.get('NEXAU_CLI_STATE_DIR', '/tmp')}/driver.log",
        filemode="a",
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--user-id", default="cyberarena")
    args = ap.parse_args()

    prompt = Path(args.prompt_file).read_text(encoding="utf-8")

    # Per-match SQLite session store (get-or-create keyed on --session-id). Lives
    # under NEXAU_CLI_STATE_DIR, which the adapter clears at install → fresh per game.
    state_dir = Path(os.environ.get("NEXAU_CLI_STATE_DIR") or (Path.home() / ".nexau"))
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "nexau.db"
    engine = SQLDatabaseEngine.from_url(f"sqlite+aiosqlite:///{db_path}")
    session_manager = SessionManager(engine=engine)

    # Full build: tools + MCP servers + llm_config (model/base_url/api_key from env).
    agent_config = AgentConfig.from_yaml(Path(args.agent))
    agent = Agent(
        config=agent_config,
        session_manager=session_manager,
        user_id=args.user_id,
        session_id=args.session_id,
    )
    response = agent.run(
        message=prompt,
        context={
            "date": "2026-07-22",
            "username": args.user_id,
            "working_directory": ".",
        },
    )
    if response:
        print(response)
    # agent.run() has committed the session, but NexAU's async SQLite engine keeps
    # background tasks alive that would otherwise hang the process. The turn is done,
    # so hard-exit (flush first) rather than wait on a graceful teardown that never
    # comes — the async supervisor respawns us with the same --session-id next chunk.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
