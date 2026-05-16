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
        if not self._can_manage_role(after.guild, role):
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
        except discord.HTTPException as exc:
            log.warning("Failed to update Booster role for %s: %s", after, exc)

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
            return None

        try:
            return await guild.create_role(
                name=BOOSTER_ROLE_NAME,
                reason="Create Booster role for server boosters",
            )
        except discord.Forbidden:
            log.warning("Cannot create Booster role in %s. Check role permissions.", guild)
        except discord.HTTPException as exc:
            log.warning("Failed to create Booster role in %s: %s", guild, exc)
        return None

    @staticmethod
    def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
        me = guild.me
        if not me or not me.guild_permissions.manage_roles:
            log.warning("Cannot manage Booster role in %s: missing Manage Roles permission.", guild)
            return False
        if role >= me.top_role:
            log.warning(
                "Cannot manage Booster role in %s: bot role must be above '%s'.",
                guild,
                role.name,
            )
            return False
        return True


async def setup(bot):
    await bot.add_cog(BoosterRoles(bot))
