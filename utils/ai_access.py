"""
Access helpers for AI chat/operator features.

These helpers let HORIZON BOT recognize trusted Discord staff from the live guild
state instead of requiring every admin to be hardcoded by user ID.
"""
from __future__ import annotations

import logging
from typing import Any

import config as bot_config
from utils.helpers import has_any_role

log = logging.getLogger(__name__)


def _configured_guilds(bot: Any) -> list[Any]:
    guilds = list(getattr(bot, "guilds", []) or [])
    configured = set(getattr(bot_config, "SERVER_ADMIN_GUILD_IDS", set()) or set())
    if configured:
        guilds = [guild for guild in guilds if getattr(guild, "id", None) in configured]
    return guilds


async def resolve_discord_admin_member(bot: Any, user_id: int) -> tuple[bool, str]:
    """
    Return whether a user is an admin/staff member in one of HORIZON BOT's guilds.

    A user is trusted when any of these is true in a configured guild:
    - they are the Discord server owner;
    - they have Administrator permission;
    - they have a role listed in ADMIN_ROLE_IDS / ADMIN_ROLE_NAMES.

    The function uses cached members first, then falls back to guild.fetch_member()
    so DM access can work even when the member cache is incomplete.
    """
    for guild in _configured_guilds(bot):
        guild_id = getattr(guild, "id", None)
        guild_name = getattr(guild, "name", "unknown guild")

        if getattr(guild, "owner_id", None) == user_id:
            return True, f"guild-owner:{guild_id}"

        member = None
        try:
            member = guild.get_member(user_id)
        except Exception:
            member = None

        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None

        if member is None:
            continue

        permissions = getattr(member, "guild_permissions", None)
        if permissions and getattr(permissions, "administrator", False):
            return True, f"administrator-permission:{guild_id}"

        if has_any_role(member, bot_config.ADMIN_ROLE_IDS, bot_config.ADMIN_ROLE_NAMES):
            return True, f"admin-role:{guild_name}"

    return False, ""


async def resolve_ai_chat_access(bot: Any, user_id: int) -> tuple[bool, str, str]:
    """
    Resolve AI chat access.

    Returns: (allowed, access_level, reason)
    access_level is one of: owner, admin, member.
    """
    if bot_config.AI_CHAT_ALLOWED_IDS and user_id in bot_config.AI_CHAT_ALLOWED_IDS:
        return True, "owner", "configured-ai-chat-id"

    if getattr(bot_config, "AI_CHAT_ALLOW_DISCORD_ADMINS", True):
        is_admin, reason = await resolve_discord_admin_member(bot, user_id)
        if is_admin:
            return True, "admin", reason

    return False, "member", ""


async def resolve_ai_operator_access(bot: Any, user_id: int) -> tuple[bool, str, str]:
    """
    Resolve AI operator access.

    By default this remains limited to AI_OPERATOR_ALLOWED_IDS. Set
    AI_OPERATOR_ALLOW_DISCORD_ADMINS=true only when server admins are allowed to
    approve/trigger whitelisted operator actions.
    """
    if bot_config.AI_OPERATOR_ALLOWED_IDS and user_id in bot_config.AI_OPERATOR_ALLOWED_IDS:
        return True, "owner", "configured-ai-operator-id"

    if getattr(bot_config, "AI_OPERATOR_ALLOW_DISCORD_ADMINS", False):
        is_admin, reason = await resolve_discord_admin_member(bot, user_id)
        if is_admin:
            return True, "admin", reason

    return False, "member", ""
