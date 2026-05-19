"""
Automatic Discord server administration audit.

This cog is read-only by default. Server-changing actions are handled through
owner-approved AI operator proposals.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import discord
from discord.ext import commands

import config as bot_config
from config import COLOR_ERROR, COLOR_INFO, COLOR_WARNING
from utils.server_admin import ServerAdminSummary, audit_servers

log = logging.getLogger(__name__)


class ServerAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._task: asyncio.Task | None = None

    async def cog_load(self):
        if bot_config.SERVER_ADMIN_ENABLED:
            self._task = asyncio.create_task(self._audit_loop())
            log.info("Server admin audit task enabled")

    async def cog_unload(self):
        if self._task:
            self._task.cancel()

    async def _audit_loop(self):
        await self.bot.wait_until_ready()
        if bot_config.SERVER_ADMIN_START_DELAY_SECONDS > 0:
            await asyncio.sleep(bot_config.SERVER_ADMIN_START_DELAY_SECONDS)

        if bot_config.SERVER_ADMIN_AUDIT_ON_START:
            await self.run_audit(automatic=True)

        interval_seconds = max(1.0, bot_config.SERVER_ADMIN_AUDIT_INTERVAL_HOURS) * 3600
        while not self.bot.is_closed():
            await asyncio.sleep(interval_seconds)
            await self.run_audit(automatic=True)

    async def run_audit(self, *, automatic: bool = False) -> ServerAdminSummary:
        summary = await audit_servers(self.bot)
        self.bot.last_server_admin_summary = summary
        if hasattr(self.bot, "record_ai_event"):
            self.bot.record_ai_event(
                "warning" if summary.has_issues else "info",
                "server_admin",
                "Discord server audit finished.",
                {"automatic": automatic, "fields": summary.to_fields()},
            )
        if hasattr(self.bot, "queue_ai_caretaker") and summary.has_issues:
            self.bot.queue_ai_caretaker(
                "server-admin-audit-issues",
                {"automatic": automatic, "fields": summary.to_fields()},
                force=False,
            )
        if automatic and summary.has_issues and bot_config.SERVER_ADMIN_ALERT_ON_ISSUES:
            await self._alert(summary)
        return summary

    def _allowed_guilds(self) -> list[discord.Guild]:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        if bot_config.SERVER_ADMIN_GUILD_IDS:
            guilds = [guild for guild in guilds if guild.id in bot_config.SERVER_ADMIN_GUILD_IDS]
        return guilds

    def _resolve_guild(self, params: dict[str, Any]) -> discord.Guild:
        guild_id = str(params.get("guild_id") or "").strip()
        guilds = self._allowed_guilds()
        if guild_id.isdigit():
            guild = discord.utils.get(guilds, id=int(guild_id))
            if guild:
                return guild
            raise ValueError(f"Configured guild `{guild_id}` is not available to the bot.")
        if len(guilds) == 1:
            return guilds[0]
        if not guilds:
            raise ValueError("No guild is available for server administration.")
        raise ValueError("Multiple guilds are available. Include `guild_id` in the proposal parameters.")

    @staticmethod
    def _normalize_channel_name(value: str) -> str:
        value = re.sub(r"<#(\d+)>", r"\1", str(value or "").strip())
        value = value.strip("#").strip()
        value = value.replace(" ", "-").lower()
        value = re.sub(r"[^a-z0-9_-]+", "-", value)
        value = re.sub(r"-{2,}", "-", value).strip("-")
        return value[:90]

    @staticmethod
    def _clean_channel_name(value: str) -> str:
        text = re.sub(r"<#(\d+)>", r"\1", str(value or "").strip())
        text = text.strip("#").strip()
        text = re.sub(r"[\r\n\t]+", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:100]

    def _resolve_text_channel(
        self,
        guild: discord.Guild,
        params: dict[str, Any],
        *,
        fallback_names: set[str] | None = None,
    ) -> discord.TextChannel:
        target = str(params.get("channel_id") or params.get("channel") or params.get("channel_name") or "").strip()
        channel = None
        mention_match = re.fullmatch(r"<#(\d+)>", target)
        if mention_match:
            target = mention_match.group(1)
        if target.isdigit():
            channel = guild.get_channel(int(target))
        if not channel and target:
            normalized = self._normalize_channel_name(target)
            channel = discord.utils.find(
                lambda item: self._normalize_channel_name(item.name) == normalized,
                guild.text_channels,
            )
        if not channel and fallback_names:
            lowered = {self._normalize_channel_name(item) for item in fallback_names}
            channel = discord.utils.find(
                lambda item: self._normalize_channel_name(item.name) in lowered,
                guild.text_channels,
            )
        if not isinstance(channel, discord.TextChannel):
            raise ValueError("Target text channel was not found.")
        return channel

    @staticmethod
    def _find_roles(
        guild: discord.Guild,
        role_ids: set[int],
        role_names: set[str],
    ) -> list[discord.Role]:
        found: list[discord.Role] = []
        seen: set[int] = set()
        lowered = {item.lower() for item in role_names}
        for role in guild.roles:
            if role.id in role_ids or role.name.lower() in lowered:
                if role.id not in seen:
                    found.append(role)
                    seen.add(role.id)
        return found

    @staticmethod
    def _require_text(params: dict[str, Any], key: str, *, max_chars: int) -> str:
        value = str(params.get(key) or "").strip()
        if not value:
            raise ValueError(f"`{key}` is required.")
        if len(value) > max_chars:
            raise ValueError(f"`{key}` is too long. Maximum is {max_chars} characters.")
        return value

    @staticmethod
    def _short_text(value: Any, limit: int = 140) -> str:
        text = str(value or "").replace("\n", " ").strip()
        if len(text) > limit:
            return text[: limit - 3] + "..."
        return text

    def _server_embed(self, *, title: str, description: str, color: int = COLOR_INFO) -> discord.Embed:
        embed = discord.Embed(
            title=title[:256],
            description=description[:4096],
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text="TriadBot")
        return embed

    @staticmethod
    def _split_text(value: str, *, limit: int = 3800) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []

        chunks: list[str] = []
        current = ""
        paragraphs = re.split(r"\n\s*\n", text)

        def add_chunk(chunk: str) -> None:
            clean = chunk.strip()
            if clean:
                chunks.append(clean)

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            if len(paragraph) > limit:
                if current:
                    add_chunk(current)
                    current = ""
                lines = paragraph.splitlines() or [paragraph]
                line_current = ""
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    if len(line) > limit:
                        if line_current:
                            add_chunk(line_current)
                            line_current = ""
                        for index in range(0, len(line), limit):
                            add_chunk(line[index : index + limit])
                        continue
                    candidate = f"{line_current}\n{line}".strip() if line_current else line
                    if len(candidate) > limit:
                        add_chunk(line_current)
                        line_current = line
                    else:
                        line_current = candidate
                if line_current:
                    add_chunk(line_current)
                continue

            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if len(candidate) > limit:
                add_chunk(current)
                current = paragraph
            else:
                current = candidate

        if current:
            add_chunk(current)
        return chunks

    @staticmethod
    def _rules_chunk_title(title: str, index: int, total: int) -> str:
        if total <= 1:
            return title[:256]
        suffix = f" ({index}/{total})"
        return f"{title[: max(1, 256 - len(suffix))]}{suffix}"

    async def send_announcement(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(
            guild,
            params,
            fallback_names=bot_config.SERVER_ADMIN_ANNOUNCEMENT_CHANNEL_NAMES,
        )
        content = self._require_text(params, "content", max_chars=30000)
        image_url = str(params.get("image_url") or "").strip()
        if image_url.startswith(("http://", "https://")):
            content = f"{content}\n\n{image_url}".strip()
        chunks = self._split_text(content, limit=1900)
        if not chunks:
            raise ValueError("Announcement content is empty after parsing.")
        if len(chunks) > 16:
            raise ValueError("Announcement content is too long. Maximum is 16 Discord messages.")

        sent_messages: list[discord.Message] = []
        for chunk in chunks:
            sent_messages.append(
                await channel.send(
                    content=chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                    suppress_embeds=True,
                )
            )
        first = sent_messages[0]
        return (
            f"Announcement sent to #{channel.name} ({channel.id}) as plain message(s). "
            f"Messages: {len(sent_messages)}. First message ID: {first.id}"
        )

    async def update_rules(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(
            guild,
            params,
            fallback_names=bot_config.SERVER_ADMIN_RULES_CHANNEL_NAMES,
        )
        content = self._require_text(params, "content", max_chars=30000)
        chunks = self._split_text(content, limit=1900)
        if not chunks:
            raise ValueError("Rules content is empty after parsing.")
        if len(chunks) > 16:
            raise ValueError("Rules content is too long. Maximum is 16 Discord messages.")

        old_messages: list[discord.Message] = []
        async for message in channel.history(limit=100):
            if not self.bot.user or message.author.id != self.bot.user.id:
                continue
            old_messages.append(message)

        for message in old_messages:
            try:
                await message.delete()
            except discord.HTTPException:
                log.warning("Could not delete old rules message %s in %s", message.id, channel, exc_info=True)

        sent_messages: list[discord.Message] = []
        for chunk in chunks:
            sent_messages.append(
                await channel.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
            )

        target_message = sent_messages[0]

        if bool(params.get("pin")):
            try:
                await target_message.pin(reason="Owner-approved rules update")
            except discord.HTTPException:
                log.warning("Could not pin rules message in %s", channel, exc_info=True)

        action = "updated" if old_messages else "posted"
        return (
            f"Rules {action} in #{channel.name} ({channel.id}). "
            f"Plain messages: {len(sent_messages)}. Characters: {len(content)}. First message ID: {target_message.id}"
        )

    async def pin_message(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(guild, params)
        message_id = str(params.get("message_id") or "").strip()
        if message_id.isdigit():
            message = await channel.fetch_message(int(message_id))
        else:
            messages = [item async for item in channel.history(limit=1)]
            if not messages:
                raise ValueError(f"No recent message found in #{channel.name} to pin.")
            message = messages[0]
        await message.pin(reason="Owner-approved server administration action")
        return f"Message {message.id} pinned in #{channel.name} ({channel.id})."

    async def set_channel_topic(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(guild, params)
        topic = self._require_text(params, "topic", max_chars=1024)
        await channel.edit(topic=topic, reason="Owner-approved channel topic update")
        return f"Topic updated for #{channel.name} ({channel.id}): {self._short_text(topic)}"

    async def configure_channel_access(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(guild, params)
        mode = str(params.get("mode") or "admin_mod_only_send").strip().lower()
        if mode not in {"admin_mod_only_send", "staff_only_send"}:
            raise ValueError(f"Unsupported channel access mode `{mode}`.")

        admin_roles = self._find_roles(guild, bot_config.ADMIN_ROLE_IDS, bot_config.ADMIN_ROLE_NAMES)
        moderator_roles = self._find_roles(guild, bot_config.MODERATOR_ROLE_IDS, bot_config.MODERATOR_ROLE_NAMES)
        staff_roles: list[discord.Role] = []
        seen: set[int] = set()
        for role in [*admin_roles, *moderator_roles]:
            if role.id not in seen:
                staff_roles.append(role)
                seen.add(role.id)

        if not staff_roles:
            raise ValueError(
                "No Admin role was found. Configure ADMIN_ROLE_IDS or make sure the role name matches "
                "ADMIN_ROLE_NAMES."
            )

        everyone = guild.default_role
        everyone_overwrite = channel.overwrites_for(everyone)
        everyone_overwrite.view_channel = True
        everyone_overwrite.read_message_history = True
        everyone_overwrite.send_messages = False
        everyone_overwrite.create_public_threads = False
        everyone_overwrite.create_private_threads = False
        everyone_overwrite.send_messages_in_threads = False
        await channel.set_permissions(
            everyone,
            overwrite=everyone_overwrite,
            reason="Owner-approved channel access configuration",
        )

        for role in staff_roles:
            role_overwrite = channel.overwrites_for(role)
            role_overwrite.view_channel = True
            role_overwrite.read_message_history = True
            role_overwrite.send_messages = True
            role_overwrite.create_public_threads = True
            role_overwrite.create_private_threads = True
            role_overwrite.send_messages_in_threads = True
            await channel.set_permissions(
                role,
                overwrite=role_overwrite,
                reason="Owner-approved channel access configuration",
            )

        bot_member = guild.me
        bot_access_note = ""
        if bot_member:
            bot_overwrite = channel.overwrites_for(bot_member)
            bot_overwrite.view_channel = True
            bot_overwrite.read_message_history = True
            bot_overwrite.send_messages = True
            bot_overwrite.create_public_threads = True
            bot_overwrite.create_private_threads = True
            bot_overwrite.send_messages_in_threads = True
            await channel.set_permissions(
                bot_member,
                overwrite=bot_overwrite,
                reason="Owner-approved channel access configuration",
            )
            bot_access_note = f" Bot access granted to {bot_member.display_name}."

        topic = str(params.get("topic") or "").strip()
        if topic:
            if len(topic) > 1024:
                raise ValueError("`topic` is too long. Maximum is 1024 characters.")
            await channel.edit(topic=topic, reason="Owner-approved channel access topic update")

        missing_groups = []
        if not admin_roles:
            missing_groups.append("Admin")
        if not moderator_roles and bot_config.MODERATOR_ROLE_REQUIRED:
            missing_groups.append("Moderator")
        role_names = ", ".join(role.name for role in staff_roles)
        note = f" Missing configured role group(s): {', '.join(missing_groups)}." if missing_groups else ""
        return (
            f"Configured #{channel.name} ({channel.id}) so everyone can read but only staff roles can send. "
            f"Allowed staff roles: {role_names}.{bot_access_note}{note}"
        )

    async def create_channel(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        raw_name = self._require_text(params, "name", max_chars=100)
        name = self._clean_channel_name(raw_name)
        if not name:
            raise ValueError("Channel name is invalid.")
        normalized_name = self._normalize_channel_name(name)
        topic = str(params.get("topic") or "").strip()[:1024] or None
        existing_channel = discord.utils.find(
            lambda item: self._normalize_channel_name(item.name) == normalized_name,
            guild.text_channels,
        )
        if existing_channel:
            if topic and topic != (existing_channel.topic or ""):
                await existing_channel.edit(topic=topic, reason="Owner-approved existing channel topic update")
                return (
                    f"Text channel #{existing_channel.name} ({existing_channel.id}) already exists; "
                    "no duplicate was created. Topic was updated instead."
                )
            return (
                f"Text channel #{existing_channel.name} ({existing_channel.id}) already exists; "
                "no duplicate was created."
            )

        category = None
        category_name = str(params.get("category") or "").strip().lower()
        if category_name:
            normalized_category = self._normalize_channel_name(category_name)
            category = discord.utils.find(
                lambda item: item.name.lower().replace(" ", "-") == normalized_category,
                guild.categories,
            )
            if not category:
                raise ValueError(f"Category `{category_name}` was not found.")

        channel = await guild.create_text_channel(
            name=name,
            topic=topic,
            category=category,
            reason="Owner-approved text channel creation",
        )
        return f"Created text channel #{channel.name} ({channel.id})."

    async def _alert(self, summary: ServerAdminSummary) -> None:
        notifier = getattr(self.bot, "notify_admins", None)
        if not notifier:
            return
        fields = summary.to_fields()
        issue_lines = []
        for audit in summary.audits:
            if not audit.has_issues:
                continue
            issue_lines.append(f"{audit.guild_name} ({audit.guild_id})")
            for item in (
                audit.missing_permissions
                + audit.role_issues
                + audit.channel_issues
                + audit.security_issues
            )[:6]:
                issue_lines.append(f"- {item}")
            if audit.boosters_missing_role:
                issue_lines.append(f"- {audit.boosters_missing_role} booster(s) missing Booster role")
            if audit.non_boosters_with_role:
                issue_lines.append(f"- {audit.non_boosters_with_role} non-booster(s) still have Booster role")
            if len("\n".join(issue_lines)) > 900:
                break
        if issue_lines:
            fields["Samples"] = "\n".join(issue_lines)[:1024]
        await notifier(
            "Discord server audit needs attention",
            "Server caretaker found permission, role, channel, Booster role, or Security Bot issues.",
            level="warning",
            fields=fields,
            key="server-admin-audit-issues",
        )

    def summary_embed(self, summary: ServerAdminSummary) -> discord.Embed:
        embed = discord.Embed(
            title="Discord Server Audit",
            description="Read-only audit finished.",
            color=COLOR_WARNING if summary.has_issues else COLOR_INFO,
            timestamp=discord.utils.utcnow(),
        )
        for name, value in summary.to_fields().items():
            embed.add_field(name=name, value=f"`{value}`", inline=True)
        samples = []
        for audit in summary.audits:
            if not audit.has_issues:
                continue
            samples.append(f"{audit.guild_name}: issues detected")
            samples.extend(
                f"- {item}"
                for item in (audit.missing_permissions + audit.role_issues + audit.security_issues)[:5]
            )
            if audit.boosters_missing_role:
                samples.append(f"- {audit.boosters_missing_role} booster(s) missing Booster role")
            if audit.non_boosters_with_role:
                samples.append(f"- {audit.non_boosters_with_role} non-booster(s) still have Booster role")
        if samples:
            embed.add_field(name="Samples", value="\n".join(samples)[:1024], inline=False)
        return embed


async def setup(bot):
    await bot.add_cog(ServerAdmin(bot))
