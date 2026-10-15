"""Arena-provisioned, match-long application identities for attackers.

Before round 0 is attackable, each opt-in victim service hands every opponent a
distinct baseline principal through `FlagHandler.provision_principals`.
Anonymous services and services whose natural self-registration grants the full
baseline authority opt out. Objectives and proof material rotate every round
around provisioned identities; the identities themselves do not.

This module owns the seed material, the validation, and the result mapping. It
deliberately holds NO flag state: assignments are keyed
`(victim_team_id, service, attacker_team_id)` and live outside `FlagStore`, so
rotation never touches them. A credential never enters an event, a record, an
error message, a target row, or a prompt; it reaches only the one attacker it was
minted for, through `/game/attack-info`.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping

from challenges.interface import BaselinePrincipal, FlagHandler

# Per-assignment seed material. 32 bytes of `secrets` entropy, independent for
# every (victim, service, attacker) — so a credential derived from it cannot be
# computed from a team id, another attacker's assignment, or anything public
# (AUTH-2). The handler decides what to derive from it.
_SEED_BYTES = 32

PrincipalKey = tuple[str, str, str]  # (victim_team_id, service, attacker_team_id)


class PrincipalProvisioningError(RuntimeError):
    """A challenge's principal provisioning failed or returned something the
    framework refuses to publish. The message is always log-safe: victim,
    service and attacker ids plus a reason — never a seed, a credential, or a
    key the handler returned."""


def new_seed() -> str:
    return secrets.token_hex(_SEED_BYTES)


def provision(
    *,
    challenge_by_owner: dict,
    target_for,
    team_ids: list[str],
) -> dict[PrincipalKey, BaselinePrincipal]:
    """Provision every opt-in challenge's baseline principals, once.

    For each deployed `(victim, service)`, hands the challenge's flag_handler the
    COMPLETE attacker set (`{attacker_team_id: seed}`, the victim excluded) in one
    call, so it can create equal-privilege identities together. A handler that
    inherits `FlagHandler.provision_principals` opts out and contributes nothing;
    this is appropriate for anonymous or naturally self-registering baselines.
    Once a handler overrides the hook, it must return the complete mapping or
    raise — an overridden hook returning `None` is a setup error.

    Raises `PrincipalProvisioningError` on ANY failure — a handler that raised, a
    partial/malformed return, two attackers on one victim sharing a principal id,
    or ANY two assignments in the whole match sharing a credential bundle (a
    bundle reused on a second victim is still one attacker able to act as
    another). The caller must not publish a flag server that failed here: a match
    cannot start with attackers who have no way in, or with identities that can't
    be told apart.
    """
    out: dict[PrincipalKey, BaselinePrincipal] = {}
    # Match-wide credential-bundle registry: bundle -> the assignment that first
    # claimed it. Global by design, so a duplicate across victims is caught too.
    bundles: dict[frozenset, PrincipalKey] = {}
    for (victim, service), challenge in sorted(challenge_by_owner.items()):
        provision_one = getattr(challenge.flag_handler, "provision_principals", None)
        if provision_one is None:
            continue
        # The inherited default is the opt-out signal. An override is an explicit
        # opt-in and must satisfy the complete-mapping contract below; in
        # particular, accidentally falling through to `None` must fail setup.
        if (
            getattr(provision_one, "__func__", None)
            is FlagHandler.provision_principals
        ):
            continue
        attackers = [t for t in team_ids if t != victim]
        if not attackers:
            continue
        seeds = {attacker: new_seed() for attacker in attackers}
        where = f"{victim}/{service}"
        try:
            granted = provision_one(target_for(victim, challenge), seeds)
        except Exception as exc:  # noqa: BLE001 — never leak a handler message
            raise PrincipalProvisioningError(
                f"{where}: provision_principals raised {type(exc).__name__}"
            ) from None
        if granted is None:
            raise PrincipalProvisioningError(
                f"{where}: overridden provision_principals must return a mapping"
            ) from None
        try:
            granted = dict(granted)
        except Exception:  # noqa: BLE001 — not a mapping at all
            raise PrincipalProvisioningError(
                f"{where}: provision_principals must return a mapping"
            ) from None
        _validate(granted, seeds, victim=victim, service=service,
                  bundles=bundles)
        for attacker, principal in granted.items():
            out[(victim, service, attacker)] = BaselinePrincipal(
                principal_id=principal.principal_id,
                credentials=dict(principal.credentials),
            )
    return out


def _validate(
    granted: dict, seeds: dict, *, victim: str, service: str, bundles: dict,
) -> None:
    """Reject anything we would otherwise publish blind. Messages name our OWN
    ids and a reason only — an unexpected key is reported as a COUNT, since its
    content is handler-supplied and could itself carry a secret."""
    where = f"{victim}/{service}"
    if set(granted) != set(seeds):
        missing = sorted(set(seeds) - set(granted))
        unexpected = len(set(granted) - set(seeds))
        raise PrincipalProvisioningError(
            f"{where}: provision_principals must cover exactly the attacker set "
            f"(missing={missing}, unexpected_keys={unexpected})"
        )
    ids: dict[str, str] = {}
    for attacker, principal in sorted(granted.items()):
        if not isinstance(principal, BaselinePrincipal):
            raise PrincipalProvisioningError(
                f"{where}: attacker {attacker} was not given a BaselinePrincipal"
            )
        if not isinstance(principal.principal_id, str) or not principal.principal_id:
            raise PrincipalProvisioningError(
                f"{where}: attacker {attacker} has an empty principal_id"
            )
        creds = principal.credentials
        if not isinstance(creds, Mapping) or not creds or not all(
            isinstance(k, str) and k and isinstance(v, str) and v
            for k, v in creds.items()
        ):
            raise PrincipalProvisioningError(
                f"{where}: attacker {attacker} has empty or non-string credentials"
            )
        # Two attackers sharing an identity or a secret means a capture can no
        # longer be attributed to one of them — refuse the whole assignment.
        if principal.principal_id in ids:
            raise PrincipalProvisioningError(
                f"{where}: attackers {ids[principal.principal_id]} and {attacker} "
                f"were given the same principal_id"
            )
        ids[principal.principal_id] = attacker
        bundle = frozenset(creds.items())
        if bundle in bundles:
            owner = bundles[bundle]
            raise PrincipalProvisioningError(
                f"{where}: attacker {attacker} was given the same credentials as "
                f"the assignment {owner[2]}@{owner[0]}/{owner[1]}"
            )
        bundles[bundle] = (victim, service, attacker)
