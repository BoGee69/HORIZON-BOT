"""
Safe Discord server administration diagnostics.

This module only inspects guild state. Mutating operations stay in cogs and are
called through owner-approved operator actions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import discord

import config as bot_config
from utils.ai_caretaker import sanitize_data


PERMISSION_LABELS = {
    "administrator": "Administrator",
    "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels",
    "manage_messages": "Manage Messages",
    "moderate_members": "Timeout Members",
    "kick_members": "Kick Members",
    "ban_members": "Ban Members",
    "view_audit_log": "View Audit Log",
    "send_messages": "Send Messages",
    "embed_links": "Embed Links",
    "read_message_history": "Read Message History",
}


@dataclass
class GuildAudit:
    guild_id: int
    guild_name: str
    member_count: int = 0
    role_count: int = 0
    text_channel_count: int = 0
    missing_permissions: list[str] = field(default_factory=list)
    role_issues: list[str] = field(default_factory=list)
    channel_issues: list[str] = field(default_factory=list)
    booster_role_found: bool = False
    boosters: int = 0
    boosters_missing_role: int = 0
    non_boosters_with_role: int = 0

    @property
    def has_issues(self) -> bool:
        return bool(
            self.missing_permissions
            or self.role_issues
            or self.channel_issues
            or self.boosters_missing_role
            or self.non_boosters_with_role
        )

    def to_dict(self) -> dict[str, Any]:
        return sanitize_data(
            {
                "guild_id": self.guild_id,
                "guild_name": self.guild_name,
                "member_count": self.member_count,
                "role_count": self.role_count,
                "text_channel_count": self.text_channel_count,
                "missing_permissions": self.missing_permissions[:12],
                "role_issues": self.role_issues[:12],
                "channel_issues": self.channel_issues[:12],
                "booster_role_found": self.booster_role_found,
                "boosters": self.boosters,
                "boosters_missing_role": self.boosters_missing_role,
                "non_boosters_with_role": self.non_boosters_with_role,
            }
        )


@dataclass
class ServerAdminSummary:
    guilds_checked: int = 0
    guilds_with_issues: int = 0
    missing_permissions: int = 0
    role_issues: int = 0
    channel_issues: int = 0
    boosters_missing_role: int = 0
    non_boosters_with_role: int = 0
    audits: list[GuildAudit] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return self.guilds_with_issues > 0

    def to_fields(self) -> dict[str, str]:
        return {
            "Guilds checked": str(self.guilds_checked),
            "Guilds with issues": str(self.guilds_with_issues),
            "Missing permissions": str(self.missing_permissions),
            "Role issues": str(self.role_issues),
            "Channel issues": str(self.channel_issues),
            "Boosters missing role": str(self.boosters_missing_role),
            "Non-boosters with role": str(self.non_boosters_with_role),
        }

    def to_dict(self) -> dict[str, Any]:
        return sanitize_data(
            {
                "fields": self.to_fields(),
                "has_issues": self.has_issues,
                "audits": [audit.to_dict() for audit in self.audits[:5]],
            }
        )


def _guild_allowed(guild: discord.Guild) -> bool:
    return not bot_config.SERVER_ADMIN_GUILD_IDS or guild.id in bot_config.SERVER_ADMIN_GUILD_IDS


def _find_role(guild: discord.Guild, role_ids: set[int], role_names: set[str]) -> discord.Role | None:
    for role in guild.roles:
        if role.id in role_ids:
            return role
    lowered = {item.lower() for item in role_names}
    return discord.utils.find(lambda role: role.name.lower() in lowered, guild.roles)


def _permission_missing(me: discord.Member) -> list[str]:
    permissions = me.guild_permissions
    if permissions.administrator:
        return []
    missing = []
    for name in sorted(bot_config.SERVER_ADMIN_REQUIRED_PERMISSIONS):
        if not getattr(permissions, name, False):
            missing.append(PERMISSION_LABELS.get(name, name))
    return missing


async def audit_guild(guild: discord.Guild) -> GuildAudit:
    me = guild.me
    audit = GuildAudit(
        guild_id=guild.id,
        guild_name=guild.name,
        member_count=guild.member_count or len(guild.members),
        role_count=len(guild.roles),
        text_channel_count=len(guild.text_channels),
    )
    if not me:
        audit.role_issues.append("Bot member object is unavailable in this guild.")
        return audit

    audit.missing_permissions = _permission_missing(me)

    booster_role = _find_role(guild, bot_config.BOOSTER_ROLE_IDS, bot_config.BOOSTER_ROLE_NAMES)
    audit.booster_role_found = booster_role is not None
    if booster_role:
        if not me.guild_permissions.administrator and not me.guild_permissions.manage_roles:
            audit.role_issues.append("Bot cannot manage Booster role because Manage Roles is missing.")
        elif booster_role >= me.top_role:
            audit.role_issues.append("Bot role is not above the Booster role.")

    for role_name, role_ids, role_names in (
        ("Admin", bot_config.ADMIN_ROLE_IDS, bot_config.ADMIN_ROLE_NAMES),
        ("Moderator", bot_config.MODERATOR_ROLE_IDS, bot_config.MODERATOR_ROLE_NAMES),
        ("Donor", bot_config.DONOR_ROLE_IDS, bot_config.DONOR_ROLE_NAMES),
        ("Booster", bot_config.BOOSTER_ROLE_IDS, bot_config.BOOSTER_ROLE_NAMES),
    ):
        role = _find_role(guild, role_ids, role_names)
        if not role and (role_ids or role_names):
            audit.role_issues.append(f"{role_name} role was not found by configured IDs/names.")

    for channel in guild.text_channels[:80]:
        perms = channel.permissions_for(me)
        missing = []
        if not perms.view_channel:
            missing.append("view")
        if not perms.send_messages:
            missing.append("send")
        if not perms.embed_links:
            missing.append("embed")
        if missing:
            audit.channel_issues.append(f"#{channel.name}: missing {', '.join(missing)}")
        if len(audit.channel_issues) >= 8:
            break

    if booster_role:
        for member in guild.members:
            if member.bot:
                continue
            has_role = booster_role in member.roles
            is_booster = bool(member.premium_since)
            if is_booster:
                audit.boosters += 1
                if not has_role:
                    audit.boosters_missing_role += 1
            elif has_role:
                audit.non_boosters_with_role += 1

    return audit


async def audit_servers(bot: Any) -> ServerAdminSummary:
    summary = ServerAdminSummary()
    for guild in getattr(bot, "guilds", []) or []:
        if not _guild_allowed(guild):
            continue
        audit = await audit_guild(guild)
        summary.audits.append(audit)
        summary.guilds_checked += 1
        if audit.has_issues:
            summary.guilds_with_issues += 1
        summary.missing_permissions += len(audit.missing_permissions)
        summary.role_issues += len(audit.role_issues)
        summary.channel_issues += len(audit.channel_issues)
        summary.boosters_missing_role += audit.boosters_missing_role
        summary.non_boosters_with_role += audit.non_boosters_with_role
    return summary
