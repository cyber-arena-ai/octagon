# Native DeepSeek Harness component

The deepseek agent provider uses DeepSeek Harness (dsh) as a native, one-shot
AgentManager. The registry selects it for either provider deepseek or the
legacy-leading cli dsh.

The agent image must already contain dsh version 0.1.0-rc.6. Installation
verifies that exact pin and fails instead of downloading packages at runtime.
DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY carry the per-team router endpoint and
principal. The generated overlay pins agent-default-model to the configured
model (default deepseek-v4-flash), disables the model-facing web tool, and,
when configured, adds the cyberarena streamable-HTTP MCP client with the team
token. failOnStartupError is true so a dead MCP cannot silently produce a
toolless agent.

Install performs one boot-free dump-config composition warmup. It is
best-effort and records only a fixed exit class; raw warmup output is neither
logged nor retained. Real phases invoke the headless profile with the prompt
as a positional argument and no stdin. The session-marker wrapper launches
the whole phase in a process group, so shared timeout cleanup reaps every DSH
child. DSH command failures remain non-zero and flow into the existing
fail-loud phase classification.

The native headless CLI creates a fresh session per invocation and exposes no
resume flag. It therefore fits the long-running asynchronous match mode; sync
phases do not share DSH conversation memory.
