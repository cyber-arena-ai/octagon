"""HttpBackend — the MCP backend the containerized gateway uses.

Same token-first contract as `gamemgr.mcp_server.GameMgrBackend`, but each
method is an HTTP call instead of an in-process `game.*` call. The gateway is
stateless (no token map): it forwards the agent's `X-Team-Token` and lets the
backend service resolve identity + authorize. Errors map to the shared
`Unauthorized` / `InvalidParams` the tool layer turns into MCP errors.

Routing (v3):
  submit_flag        -> flag-service /game/flag/submit   (DIRECT; GameMgr not in path)
  claim_capability   -> flag-service /game/flag/claim    (DIRECT; GameMgr not in path)
  get_attack_targets -> flag-service /game/attack-info   (DIRECT; GameMgr not in path)
                        (forwards both `targets` and `principals`)
  everything else    -> GameMgr REST (status / attacks / report / restart)
"""

from __future__ import annotations

import httpx

from gamemgr.mcp_server import InvalidParams, Unauthorized


class HttpBackend:
    def __init__(
        self,
        gm_base_url: str,
        flag_service_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._gm = gm_base_url.rstrip("/")
        self._fs = flag_service_url.rstrip("/")
        self._http = client or httpx.AsyncClient(timeout=30.0)

    async def submit_flag(self, token: str, flag: str) -> dict:
        # Direct to the flag-service: it maps token->team itself and records
        # FLAG_SUBMITTED via its EventSink. GameMgr is never consulted.
        return await self._post_flag_action(
            "/game/flag/submit", token, {"flag": flag},
            unavailable_reason="ERROR", retry_action="submission",
        )

    async def claim_capability(
        self, token: str, victim_team_id: str, service: str, store: str,
    ) -> dict:
        # Direct to the flag-service (like submit_flag): it reads the victim's
        # protected region itself and records FLAG_SUBMITTED. GameMgr is never
        # consulted.
        # OBSERVE_ERROR is the claim vocabulary's retryable "could not tell"
        # verdict; ERROR belongs to the submit vocabulary.
        return await self._post_flag_action(
            "/game/flag/claim", token,
            {"victim_team_id": victim_team_id, "service": service,
             "store": store},
            unavailable_reason="OBSERVE_ERROR", retry_action="claim",
        )

    async def _post_flag_action(
        self, path: str, token: str, body: dict, *,
        unavailable_reason: str, retry_action: str,
    ) -> dict:
        """POST one player flag action, preserving its retry vocabulary."""
        try:
            r = await self._http.post(
                f"{self._fs}{path}", json=body,
                headers={"X-Team-Token": token},
            )
            r.raise_for_status()
            d = r.json()
        except httpx.HTTPError:
            # A slow/unreachable flag service is retryable rather than an opaque
            # MCP error. The caller supplies the action-specific reason/detail.
            return {"accepted": False, "reason": unavailable_reason,
                    "detail": "flag service temporarily unavailable — "
                              f"retry this {retry_action}"}
        # Preserve the host MCP's no-oracle behavior: a bad token is a generic
        # Unauthorized, not a BAD_TOKEN reason on the wire.
        if d.get("reason") == "BAD_TOKEN":
            raise Unauthorized()
        return {"accepted": d.get("accepted"), "reason": d.get("reason"),
                "detail": d.get("detail", "")}

    async def get_attack_targets(self, token: str) -> dict:
        # Direct to the flag-service (like submit_flag): it holds the plant
        # handles the public flag-ids are derived from and maps token->team
        # itself. GameMgr is never consulted, so flag-ids stay off its timeline.
        try:
            r = await self._http.post(
                f"{self._fs}/game/attack-info", headers={"X-Team-Token": token},
            )
            r.raise_for_status()
            d = r.json()
        except httpx.HTTPError:
            # Flag-service slow/unreachable (e.g. busy planting at a rotation):
            # a retryable-empty is better than an opaque MCP error.
            return {"targets": [], "principals": []}
        if d.get("error") == "BAD_TOKEN":
            raise Unauthorized()
        # `principals` is forwarded verbatim alongside `targets`; a caller that
        # only reads `targets` is unaffected.
        return {"targets": d.get("targets", []),
                "principals": d.get("principals", [])}

    async def get_game_state(self, token: str) -> dict:
        # /game/state (token-scoped) mirrors the tokenless /game/status but lets
        # GameMgr record MCP_STATE_QUERY for the agent's poll (vs facility status
        # reads on /game/status). Content is identical + team-agnostic.
        return await self._get(f"{self._gm}/game/state", token)

    async def get_attacks_received(self, token: str) -> dict:
        return await self._get(f"{self._gm}/game/attacks", token)

    async def report_ready(self, token: str) -> dict:
        return await self._post(f"{self._gm}/game/report/ready", token, {})

    async def report_deploy_complete(self, token: str, service: str) -> dict:
        return await self._post(f"{self._gm}/game/report/deploy-complete", token,
                                {"service": service})

    async def restart_service(self, token: str, service: str) -> dict:
        return await self._post(f"{self._gm}/game/restart", token,
                                {"service": service})

    async def trigger_health_check(self, token: str, service: str | None) -> dict:
        return await self._post(f"{self._gm}/game/health/trigger", token,
                                {"service": service} if service else {})

    async def get_health_checks(self, token: str, limit: int | None) -> dict:
        url = f"{self._gm}/game/health/recent"
        if limit is not None:
            url = f"{url}?limit={int(limit)}"
        return await self._get(url, token)

    async def get_flag_checks(self, token: str) -> dict:
        return await self._get(f"{self._gm}/game/flags/checks", token)

    # ── helpers: forward X-Team-Token, map 401/400 to the shared errors ──────

    async def _get(self, url: str, token: str) -> dict:
        return self._handle(await self._http.get(url, headers={"X-Team-Token": token}))

    async def _post(self, url: str, token: str, body: dict) -> dict:
        return self._handle(
            await self._http.post(url, json=body, headers={"X-Team-Token": token})
        )

    @staticmethod
    def _handle(r: httpx.Response) -> dict:
        if r.status_code == 401:
            raise Unauthorized()
        if r.status_code == 400:
            detail = "invalid"
            try:
                detail = r.json().get("detail", detail)
            except Exception:
                pass
            raise InvalidParams(detail)
        r.raise_for_status()
        return r.json()
