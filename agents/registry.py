"""Agent registry — pick an AgentManager subclass by provider.

Resolution order:
  1. `cfg.provider` (canonical)
  2. first whitespace token of `cfg.cli` (back-compat for yamls
     without a provider field)
  3. DEFAULT (ClaudeAgentManager) so test stand-ins like `cli: sleep 60`
     keep working

Future CLIs register by adding one line to REGISTRY.
"""

from __future__ import annotations

from models.agent import AgentConfig

from .claude import ClaudeAgentManager
from .codex import CodexAgentManager
from .deepseek import DeepSeekAgentManager
from .gemini import GeminiAgentManager
from .grok import GrokAgentManager
from .idle import IdleAgentManager
from .kimi import KimiAgentManager
from .manager import AgentManager
from .nexau import NexauAgentManager
from .openhands import OpenHandsAgentManager
from .opencode import OpenCodeAgentManager
from .qwen import QwenCodeAgentManager
from .script import ScriptAgentManager

REGISTRY: dict[str, type[AgentManager]] = {
    "claude": ClaudeAgentManager,
    "codex": CodexAgentManager,
    "deepseek": DeepSeekAgentManager,
    "dsh": DeepSeekAgentManager,
    "gemini": GeminiAgentManager,
    "grok": GrokAgentManager,
    "nexau": NexauAgentManager,
    "openhands": OpenHandsAgentManager,
    "opencode": OpenCodeAgentManager,
    "qwen": QwenCodeAgentManager,
    "script": ScriptAgentManager,
    "idle": IdleAgentManager,
    "kimi": KimiAgentManager,
}

DEFAULT: type[AgentManager] = ClaudeAgentManager


def resolve_cls(cfg: AgentConfig) -> type[AgentManager]:
    """The AgentManager subclass for `cfg` (see module docstring for
    resolution order), DEFAULT on an unknown key. No instantiation — safe
    to inspect class attributes (e.g. `CANONICAL_CLI`) without spinning up
    a Docker client."""
    key = (cfg.provider or "").strip()
    if not key and cfg.cli:
        key = cfg.cli.split(None, 1)[0]
    return REGISTRY.get(key, DEFAULT)


def expected_image(cfg: AgentConfig) -> str:
    """The agent image the adapter for `cfg` is baked into (`AgentManager.IMAGE`):
    `cyberarena/agent-<provider>` for a real harness, `cyberarena/agent-base` for
    script/idle. Config load compares the team's dev node image against this."""
    return resolve_cls(cfg).IMAGE


AGENT_IMAGE_PREFIX = "cyberarena/agent-"
AGENT_DOCKERFILES_DIR = "dockerfiles/agent"


def agent_images() -> dict[str, str]:
    """Every agent image the registry can ask for, mapped to the directory its
    Dockerfile lives in (relative to the repo root): `cyberarena/agent-base` ->
    `dockerfiles/agent/base`, `cyberarena/agent-claude` -> `dockerfiles/agent/claude`,
    ... Drives `scripts/build_images.sh`'s expectations and main.py's stale-image
    check; tests assert each entry has a Dockerfile on disk and vice versa."""
    images = {cls.IMAGE for cls in REGISTRY.values()} | {AgentManager.IMAGE}
    out: dict[str, str] = {}
    for image in sorted(images):
        assert image.startswith(AGENT_IMAGE_PREFIX), image
        out[image] = f"{AGENT_DOCKERFILES_DIR}/{image[len(AGENT_IMAGE_PREFIX):]}"
    return out


def build(cfg: AgentConfig) -> AgentManager:
    """Look up the AgentManager class for `cfg` (see module docstring
    for resolution order) and instantiate it."""
    return resolve_cls(cfg)()
