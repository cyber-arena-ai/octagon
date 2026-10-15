"""OpenHands SDK driver — one phase of a cyberarena agent, resumable across runs.

Invoked per phase by OpenHandsAgentManager.phase_command as:

    python driver.py --prompt-file <PROMPT_PATH>

Everything else comes from the environment (written to openhands.env by the adapter):
  LLM_MODEL / LLM_BASE_URL / LLM_API_KEY   — the OpenAI-compatible provider (api-router)
  CYBERARENA_MCP_ENDPOINT                  — the remote MCP server URL (optional)
  CYBERARENA_TEAM_TOKEN                    — the X-Team-Token header value
  OPENHANDS_WORKSPACE                      — cwd for tool execution (LocalWorkspace)
  OPENHANDS_PERSIST_DIR                    — conversation persistence dir (cross-phase memory)
  OPENHANDS_CONVO_ID                       — fixed conversation id (get-or-resume)

Why the SDK and not `openhands --headless`: headless CLI mode does NOT support session
resume, but our per-phase model needs cross-phase memory. Re-using a fixed
`conversation_id` + `persistence_dir` in a fresh process resumes the prior phase's
conversation (documented SDK behavior). Tools run via LocalWorkspace = direct
subprocess (no Docker-in-Docker inside the agent container).

MCP tool naming: the SDK exposes remote MCP tools to the model under their BARE server
name (MCPToolDefinition.name == mcp_tool.name, no server prefix), matching the codex/
nexau flattening, so the arena prompt's bare tool references are correct.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid

from pydantic import SecretStr

from openhands.sdk import LLM, Agent, Conversation
from openhands.sdk.mcp import MCPServer
from openhands.sdk.tool import Tool
from openhands.tools.file_editor import FileEditorTool
from openhands.tools.terminal import TerminalTool


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.stderr.write(f"openhands driver: required env {name} is unset\n")
        sys.exit(2)
    return val


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-file", required=True)
    args = ap.parse_args()

    with open(args.prompt_file, encoding="utf-8") as f:
        prompt = f.read()

    llm = LLM(
        usage_id="agent",
        model=_require("LLM_MODEL"),
        base_url=os.environ.get("LLM_BASE_URL") or None,
        api_key=SecretStr(_require("LLM_API_KEY")),
    )

    tools = [
        Tool(name=TerminalTool.name),    # autonomous shell (ssh/exploit/curl)
        Tool(name=FileEditorTool.name),  # read/write/edit service source for defense
    ]

    mcp_config: dict[str, MCPServer] = {}
    mcp_url = os.environ.get("CYBERARENA_MCP_ENDPOINT")
    if mcp_url:
        headers = {}
        token = os.environ.get("CYBERARENA_TEAM_TOKEN")
        if token:
            headers["X-Team-Token"] = SecretStr(token)
        mcp_config["cyberarena"] = MCPServer(
            url=mcp_url, transport="streamable-http", headers=headers or None,
        )

    agent = Agent(llm=llm, tools=tools, mcp_config=mcp_config)

    conversation = Conversation(
        agent=agent,
        workspace=os.environ.get("OPENHANDS_WORKSPACE") or os.getcwd(),
        persistence_dir=os.environ.get("OPENHANDS_PERSIST_DIR") or None,
        # Fixed id + persistence_dir => resume the prior phase's conversation.
        conversation_id=uuid.UUID(_require("OPENHANDS_CONVO_ID")),
    )
    conversation.send_message(prompt)
    conversation.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
