"""
Automatically manage the Booster role for Discord server boosters.
"""
import logging

import discord
from discord.ext import commands

from config import BOOSTER_ROLE_IDS, BOOSTER_ROLE_NAME, BOOSTER_ROLE_NAMES

log = logging.getLogger(__name__)


class BoosterRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since == after.premium_since:
            return

        role = await self._get_or_create_booster_role(after.guild)
        if not role:
            return
        if not await self._can_manage_role(after.guild, role):
            return

        try:
            if after.premium_since and role not in after.roles:
                await after.add_roles(role, reason="Server boost detected")
                log.info("Added Booster role to %s in %s", after, after.guild)
            elif not after.premium_since and role in after.roles:
                await after.remove_roles(role, reason="Server boost ended")
                log.info("Removed Booster role from %s in %s", after, after.guild)
        except discord.Forbidden:
            log.warning(
                "Cannot manage Booster role in %s. Check Manage Roles permission and role hierarchy.",
                after.guild,
            )
            await self._alert(
                "Booster role update failed",
                "Discord rejected the Booster role update.",
                after.guild,
                {"Member": f"{after} ({after.id})", "Reason": "Forbidden"},
            )
        except discord.HTTPException as exc:
            log.warning("Failed to update Booster role for %s: %s", after, exc)
            await self._alert(
                "Booster role update failed",
                "Discord returned an HTTP error while updating Booster role.",
                after.guild,
                {"Member": f"{after} ({after.id})", "Error": str(exc)},
            )

    async def _get_or_create_booster_role(self, guild: discord.Guild) -> discord.Role | None:
        role = discord.utils.find(
            lambda item: item.id in BOOSTER_ROLE_IDS,
            guild.roles,
        )
        if role:
            return role

        role = discord.utils.find(
            lambda item: item.name.lower() in BOOSTER_ROLE_NAMES,
            guild.roles,
        )
        if role:
            return role

        me = guild.me
        if not me or not me.guild_permissions.manage_roles:
            log.warning("Cannot create Booster role in %s: missing Manage Roles permission.", guild)
            await self._alert(
                "Booster role create failed",
                "Bot cannot create the Booster role because Manage Roles is missing.",
                guild,
                {"Required fix": "Enable Manage Roles for the bot role."},
            )
            return None

        try:
            return await guild.create_role(
                name=BOOSTER_ROLE_NAME,
                reason="Create Booster role for server boosters",
            )
        except discord.Forbidden:
            log.warning("Cannot create Booster role in %s. Check role permissions.", guild)
            await self._alert(
                "Booster role create failed",
                "Discord rejected Booster role creation.",
                guild,
                {"Required fix": "Check Manage Roles permission and role hierarchy."},
            )
        except discord.HTTPException as exc:
            log.warning("Failed to create Booster role in %s: %s", guild, exc)
            await self._alert(
                "Booster role create failed",
                "Discord returned an HTTP error while creating Booster role.",
                guild,
                {"Error": str(exc)},
            )
        return None

    async def _can_manage_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        me = guild.me
        if not me or not me.guild_permissions.manage_roles:
            log.warning("Cannot manage Booster role in %s: missing Manage Roles permission.", guild)
            await self._alert(
                "Booster role permission problem",
                "Bot cannot manage Booster role because Manage Roles is missing.",
                guild,
                {"Role": f"{role.name} ({role.id})"},
            )
            return False
        if role >= me.top_role:
            log.warning(
                "Cannot manage Booster role in %s: bot role must be above '%s'.",
                guild,
                role.name,
            )
            await self._alert(
                "Booster role hierarchy problem",
                "Bot cannot manage Booster role because the bot role is not above it.",
                guild,
                {
                    "Booster role": f"{role.name} ({role.id})",
                    "Bot top role": f"{me.top_role.name} ({me.top_role.id})",
                    "Required fix": "Move the bot role above the Booster role.",
                },
            )
            return False
        return True

    async def _alert(self, title: str, description: str, guild: discord.Guild, fields: dict[str, str]):
        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return
        payload = {"Guild": f"{guild.name} ({guild.id})"}
        payload.update(fields)
        await notifier(
            title,
            description,
            level="error",
            fields=payload,
            key=f"{title}-{guild.id}",
        )


async def setup(bot):
    await bot.add_cog(BoosterRoles(bot))
