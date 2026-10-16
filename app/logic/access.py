"""What the agent may reach: its own Lyndrix identity and allowed Vault spaces.

The assistant gets a dedicated Lyndrix user (``ai-assistant``, system flag
``bot``) with an API key the plugin issues once and keeps in the plugin's Vault
space. The runner container receives ``LYNDRIX_API_URL`` / ``LYNDRIX_API_KEY``
so Claude Code can call the Lyndrix REST API with exactly the permissions the
operator grants that user — roles and groups are managed in the normal Users
page, nothing is hard-wired here beyond the plugin's own ``agent`` role.

Vault spaces: the operator lists the KV spaces (paths below the ``lyndrix``
mount, e.g. ``plugins/lyndrix.plugin.docker``) the agent may read. The agent
fetches them through this plugin's API; every read is logged.

TODO(agent): user/api-key creation and arbitrary Vault reads use core-internal
modules (``core.components.auth.logic.*``, ``core.components.vault.logic.kv_helper``)
because ``core.api`` has no surface for them yet — expose them there and drop
these imports.
"""
from __future__ import annotations

import secrets
from typing import Iterable, Optional

from core.api import get_logger

log = get_logger("Plugin:AI:Access")

ASSISTANT_USERNAME = "ai-assistant"
ASSISTANT_ROLE = "role:lyndrix.plugin.ai_assistant:agent"
API_KEY_LABEL = "lyndrix-ai-runner"


def ensure_assistant_user() -> dict:
    """Create the bot user if missing; returns ``{username, created, roles}``. Blocking."""
    from core.components.auth.logic.hashing import hash_password
    from core.components.auth.logic.models import User
    from core.components.database.logic.db_service import db_instance

    with db_instance.SessionLocal() as s:
        user = s.query(User).filter(User.username == ASSISTANT_USERNAME).first()
        if user is not None:
            roles = list(user.roles or [])
            changed = False
            if "bot" not in roles:
                roles.append("bot")
                changed = True
            if ASSISTANT_ROLE not in roles:
                roles.append(ASSISTANT_ROLE)
                changed = True
            if changed:
                user.roles = sorted(set(roles))
                s.commit()
            return {"username": ASSISTANT_USERNAME, "created": False, "roles": sorted(set(roles))}
        # A random password nobody knows: the account is API-key only.
        user = User(
            username=ASSISTANT_USERNAME,
            hashed_password=hash_password(secrets.token_urlsafe(32)),
            full_name="Lyndrix KI-Assistent",
            email=None,
            roles=["bot", ASSISTANT_ROLE],
            groups=[],
            extra_permissions=[],
        )
        s.add(user)
        s.commit()
    log.info(f"Created the assistant account '{ASSISTANT_USERNAME}' (bot).")
    return {"username": ASSISTANT_USERNAME, "created": True, "roles": ["bot", ASSISTANT_ROLE]}


def issue_api_key() -> str:
    """Mint a fresh API key for the assistant (old keys with our label are revoked). Blocking."""
    from core.components.auth.logic.api_key_service import api_key_service

    try:
        for info in api_key_service.list_for_user(ASSISTANT_USERNAME):
            if getattr(info, "label", "") == API_KEY_LABEL and not getattr(info, "revoked", False):
                api_key_service.revoke(info.id, ASSISTANT_USERNAME)
    except Exception as exc:  # noqa: BLE001 — an unrevoked old key is not fatal
        log.debug(f"Could not revoke previous assistant keys: {exc}")
    raw, _info = api_key_service.create(ASSISTANT_USERNAME, label=API_KEY_LABEL)
    return raw


def parse_vault_spaces(raw: Optional[str]) -> list[str]:
    """``"plugins/a, plugins/b"`` → ``["plugins/a", "plugins/b"]`` (no traversal, no empties)."""
    out: list[str] = []
    for part in str(raw or "").replace("\n", ",").split(","):
        p = part.strip().strip("/")
        if p and ".." not in p and p not in out:
            out.append(p)
    return out


def read_vault_space(space: str, allowed: Iterable[str]) -> dict:
    """Key/values of one allowed space (KeyError when not allowed). Blocking."""
    space = str(space or "").strip().strip("/")
    if not space or space not in set(allowed):
        raise KeyError(space)
    from core.components.vault.logic.kv_helper import read_secret_dict

    return read_secret_dict(space) or {}
