"""
Automatic Discord server administration audit.

This cog is read-only by default. Server-changing actions are handled through
owner-approved AI operator proposals.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import timedelta
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
        chunks = self._split_text(content, limit=1900)
        if not chunks:
            raise ValueError("Announcement content is empty after parsing.")
        if len(chunks) > 16:
            raise ValueError("Announcement content is too long. Maximum is 16 Discord messages.")

        image_url = str(params.get("image_url") or "").strip()
        sent_messages: list[discord.Message] = []
        for index, chunk in enumerate(chunks):
            if index == len(chunks) - 1 and image_url.startswith(("http://", "https://")):
                chunk = f"{chunk}\n{image_url}".strip()
            sent_messages.append(
                await channel.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
            )
        return (
            f"Announcement sent to #{channel.name} ({channel.id}) as plain message(s). "
            f"Messages: {len(sent_messages)}. First message ID: {sent_messages[0].id}"
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
        channel = discord.utils.find(
            lambda item: self._normalize_channel_name(item.name) == normalized_name,
            guild.text_channels,
        )

        category = await self._ensure_category(guild, str(params.get("category") or ""))
        topic = str(params.get("topic") or "").strip()[:1024] or None
        created = False

        if channel:
            edit_kwargs: dict[str, Any] = {}
            if topic and channel.topic != topic:
                edit_kwargs["topic"] = topic
            if category and channel.category_id != category.id:
                edit_kwargs["category"] = category
            if edit_kwargs:
                await channel.edit(**edit_kwargs, reason="Owner-approved existing channel configuration")
        else:
            channel = await guild.create_text_channel(
                name=name,
                topic=topic,
                category=category,
                reason="Owner-approved text channel creation",
            )
            created = True

        access_mode = str(params.get("access_mode") or params.get("mode") or "").strip().lower()
        access_note = ""
        if access_mode in {"admin_mod_only_send", "staff_only_send"}:
            access_note = " " + await self.configure_channel_access({**params, "channel_id": str(channel.id), "mode": access_mode})

        verb = "Created" if created else "Configured existing"
        return f"{verb} text channel #{channel.name} ({channel.id}).{access_note}"

    async def _ensure_category(self, guild: discord.Guild, value: str) -> discord.CategoryChannel | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        normalized = self._normalize_channel_name(raw)
        category = discord.utils.find(
            lambda item: self._normalize_channel_name(item.name) == normalized,
            guild.categories,
        )
        if category:
            return category
        return await guild.create_category(name=raw[:100], reason="Owner-approved category setup")

    def _template_channels(self, template: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        key = self._normalize_channel_name(template or "")
        if key in {"game", "games", "game-category", "gaming"}:
            return (
                [
                    {"name": "info", "topic": "Game information and pinned resources."},
                    {"name": "download-links", "topic": "Approved game download resources."},
                    {"name": "support", "topic": "Help and troubleshooting."},
                    {"name": "chat", "topic": "General discussion for this game/category."},
                ],
                [
                    {"name": "Voice 1", "topic": ""},
                    {"name": "Voice 2", "topic": ""},
                ],
            )
        if key in {"support", "help"}:
            return (
                [
                    {"name": "support", "topic": "Support requests and troubleshooting."},
                    {"name": "faq", "topic": "Frequently asked questions."},
                    {"name": "known-issues", "topic": "Known issues and fixes."},
                ],
                [],
            )
        if key in {"community", "standard", "server"}:
            return (
                [
                    {"name": "rules", "topic": "Read before using the server."},
                    {"name": "announcement", "topic": "Official announcements."},
                    {"name": "resources", "topic": "Useful links and resources."},
                    {"name": "general", "topic": "General discussion."},
                ],
                [{"name": "General Voice", "topic": ""}],
            )
        raise ValueError("Unsupported template. Use `game`, `support`, or `community`.")

    async def setup_channel_template(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        template = str(params.get("template") or "game").strip().lower()
        category_name = str(params.get("category") or params.get("name") or template).strip()
        category = await self._ensure_category(guild, category_name)
        if not category:
            raise ValueError("`category` is required for channel template setup.")

        text_defs, voice_defs = self._template_channels(template)
        prefix = self._normalize_channel_name(str(params.get("prefix") or ""))
        access_mode = str(params.get("access_mode") or params.get("mode") or "admin_mod_only_send").strip().lower()

        created_text = configured_text = created_voice = configured_voice = 0
        results: list[str] = []

        for item in text_defs:
            base_name = item["name"]
            name = self._normalize_channel_name(f"{prefix}-{base_name}" if prefix else base_name)
            channel = discord.utils.find(
                lambda ch: self._normalize_channel_name(ch.name) == name,
                guild.text_channels,
            )
            if channel:
                await channel.edit(category=category, topic=item.get("topic") or None, reason="Owner-approved template configuration")
                configured_text += 1
            else:
                channel = await guild.create_text_channel(
                    name=name,
                    topic=item.get("topic") or None,
                    category=category,
                    reason="Owner-approved channel template setup",
                )
                created_text += 1
            if access_mode in {"admin_mod_only_send", "staff_only_send"} and name in {"rules", "announcement", "download-links", "info"}:
                await self.configure_channel_access({"guild_id": str(guild.id), "channel_id": str(channel.id), "mode": access_mode})
            results.append(f"#{channel.name}")

        for item in voice_defs:
            base_name = item["name"]
            name = f"{prefix} {base_name}".strip() if prefix else base_name
            normalized = self._normalize_channel_name(name)
            channel = discord.utils.find(
                lambda ch: self._normalize_channel_name(ch.name) == normalized,
                guild.voice_channels,
            )
            if channel:
                await channel.edit(category=category, reason="Owner-approved template configuration")
                configured_voice += 1
            else:
                channel = await guild.create_voice_channel(
                    name=name[:100],
                    category=category,
                    reason="Owner-approved channel template setup",
                )
                created_voice += 1
            results.append(f"🔊 {channel.name}")

        return (
            f"Template `{template}` applied to category `{category.name}`. "
            f"Text created={created_text}, text configured={configured_text}, "
            f"voice created={created_voice}, voice configured={configured_voice}. "
            f"Channels: {', '.join(results[:12])}"
        )

    @staticmethod
    def _parse_discord_color(value: str) -> discord.Color | None:
        text = str(value or "").strip().lower().lstrip("#")
        if not text:
            return None
        named = {
            "red": discord.Color.red(),
            "green": discord.Color.green(),
            "blue": discord.Color.blue(),
            "purple": discord.Color.purple(),
            "orange": discord.Color.orange(),
            "gold": discord.Color.gold(),
            "yellow": discord.Color.gold(),
            "teal": discord.Color.teal(),
            "grey": discord.Color.greyple(),
            "gray": discord.Color.greyple(),
        }
        if text in named:
            return named[text]
        if re.fullmatch(r"[0-9a-f]{6}", text):
            return discord.Color(int(text, 16))
        raise ValueError("Unsupported color. Use a hex value like #5865F2 or a simple color name.")

    def _resolve_role(self, guild: discord.Guild, params: dict[str, Any]) -> discord.Role:
        role_id = str(params.get("role_id") or "").strip()
        role_name = str(params.get("role") or params.get("role_name") or params.get("name") or "").strip()
        role = None
        if role_id.isdigit():
            role = guild.get_role(int(role_id))
        if not role and role_name:
            normalized = role_name.lower().lstrip("@")
            role = discord.utils.find(lambda item: item.name.lower() == normalized, guild.roles)
        if not role:
            raise ValueError("Target role was not found.")
        return role

    def _assert_role_manageable(self, guild: discord.Guild, role: discord.Role) -> None:
        me = guild.me
        if not me or not me.guild_permissions.manage_roles and not me.guild_permissions.administrator:
            raise ValueError("TriadBot is missing Manage Roles permission.")
        if role >= me.top_role or role.is_default() or role.managed:
            raise ValueError("That role cannot be managed by TriadBot because of role hierarchy or Discord restrictions.")

    async def create_role(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        name = self._require_text(params, "name", max_chars=100)
        existing = discord.utils.find(lambda item: item.name.lower() == name.lower(), guild.roles)
        color = self._parse_discord_color(str(params.get("color") or ""))
        reason = "Owner-approved role creation"
        if existing:
            updates: dict[str, Any] = {}
            if color is not None:
                updates["color"] = color
            if "hoist" in params:
                updates["hoist"] = bool(params.get("hoist"))
            if "mentionable" in params:
                updates["mentionable"] = bool(params.get("mentionable"))
            if updates:
                self._assert_role_manageable(guild, existing)
                await existing.edit(reason=reason, **updates)
            return f"Configured existing role @{existing.name} ({existing.id})."
        role = await guild.create_role(
            name=name,
            color=color or discord.Color.default(),
            hoist=bool(params.get("hoist", False)),
            mentionable=bool(params.get("mentionable", False)),
            reason=reason,
        )
        return f"Created role @{role.name} ({role.id})."

    async def update_role(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        role = self._resolve_role(guild, params)
        self._assert_role_manageable(guild, role)
        updates: dict[str, Any] = {}
        new_name = str(params.get("new_name") or "").strip()
        if new_name:
            updates["name"] = new_name[:100]
        if str(params.get("color") or "").strip():
            color = self._parse_discord_color(str(params.get("color") or ""))
            if color is not None:
                updates["color"] = color
        if "hoist" in params:
            updates["hoist"] = bool(params.get("hoist"))
        if "mentionable" in params:
            updates["mentionable"] = bool(params.get("mentionable"))
        if not updates:
            raise ValueError("No supported role updates were provided.")
        await role.edit(reason="Owner-approved role update", **updates)
        return f"Updated role @{role.name} ({role.id})."

    async def delete_role(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        role = self._resolve_role(guild, params)
        name = role.name
        role_id = role.id
        self._assert_role_manageable(guild, role)
        await role.delete(reason="Owner-approved role deletion")
        return f"Deleted role @{name} ({role_id})."

    async def _resolve_member(self, guild: discord.Guild, params: dict[str, Any]) -> discord.Member:
        raw = str(params.get("member_id") or params.get("user_id") or params.get("member") or "").strip()
        match = re.search(r"(\d{16,25})", raw)
        if match:
            member_id = int(match.group(1))
            member = guild.get_member(member_id)
            if member:
                return member
            return await guild.fetch_member(member_id)
        name = raw.lower().lstrip("@")
        member = discord.utils.find(
            lambda item: item.name.lower() == name or item.display_name.lower() == name,
            guild.members,
        )
        if member:
            return member
        raise ValueError("Target member was not found. Use a mention or user ID.")

    @staticmethod
    def _parse_duration_seconds(value: Any, default: int = 600) -> int:
        text = str(value or "").strip().lower()
        if not text:
            return default
        match = re.fullmatch(r"(\d+)\s*(s|sec|secs|second|seconds|m|min|minute|minutes|h|hour|hours|d|day|days)?", text)
        if not match:
            raise ValueError("Unsupported duration. Use values like 10m, 1h, or 1d.")
        amount = int(match.group(1))
        unit = match.group(2) or "m"
        multiplier = 1 if unit.startswith("s") else 60 if unit.startswith("m") else 3600 if unit.startswith("h") else 86400
        return max(1, min(amount * multiplier, 28 * 86400))

    async def timeout_member(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        member = await self._resolve_member(guild, params)
        seconds = self._parse_duration_seconds(params.get("duration") or params.get("duration_seconds"), default=600)
        reason = str(params.get("reason") or "Owner-approved timeout")[:512]
        until = discord.utils.utcnow() + timedelta(seconds=seconds)
        await member.timeout(until, reason=reason)
        return f"Timed out {member} ({member.id}) for {seconds} seconds."

    async def kick_member(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        member = await self._resolve_member(guild, params)
        reason = str(params.get("reason") or "Owner-approved kick")[:512]
        await member.kick(reason=reason)
        return f"Kicked {member} ({member.id})."

    async def ban_member(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        member = await self._resolve_member(guild, params)
        reason = str(params.get("reason") or "Owner-approved ban")[:512]
        delete_seconds = self._parse_duration_seconds(params.get("delete_message_duration") or "0s", default=0)
        await guild.ban(member, reason=reason, delete_message_seconds=delete_seconds)
        return f"Banned {member} ({member.id})."

    async def create_webhook(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        channel = self._resolve_text_channel(guild, params)
        name = str(params.get("name") or "TriadBot Webhook").strip()[:80] or "TriadBot Webhook"
        webhook = await channel.create_webhook(name=name, reason="Owner-approved webhook creation")
        return f"Created webhook `{webhook.name}` in #{channel.name}. Webhook ID: {webhook.id}. Token is not shown."

    async def delete_webhook(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        webhook_id = str(params.get("webhook_id") or params.get("id") or "").strip()
        if not webhook_id.isdigit():
            raise ValueError("`webhook_id` is required.")
        for channel in guild.text_channels:
            try:
                webhooks = await channel.webhooks()
            except discord.HTTPException:
                continue
            webhook = discord.utils.get(webhooks, id=int(webhook_id))
            if webhook:
                name = webhook.name
                await webhook.delete(reason="Owner-approved webhook deletion")
                return f"Deleted webhook `{name}` ({webhook_id})."
        raise ValueError("Webhook was not found in this guild.")

    async def update_server_settings(self, params: dict[str, Any]) -> str:
        guild = self._resolve_guild(params)
        updates: dict[str, Any] = {}
        name = str(params.get("name") or "").strip()
        description = str(params.get("description") or "").strip()
        if name:
            updates["name"] = name[:100]
        if description:
            updates["description"] = description[:120]
        if not updates:
            raise ValueError("No supported server setting was provided. Supported: name, description.")
        await guild.edit(reason="Owner-approved server settings update", **updates)
        changed = ", ".join(updates.keys())
        return f"Updated server setting(s): {changed}."

    def _invalidate_server_knowledge(self, reason: str, payload: dict[str, Any] | None = None) -> None:
        setattr(self.bot, "ai_server_knowledge_cache", None)
        if hasattr(self.bot, "record_ai_event"):
            self.bot.record_ai_event("info", "server_admin", f"Server knowledge cache invalidated: {reason}", payload or {})
        if hasattr(self.bot, "queue_ai_caretaker"):
            self.bot.queue_ai_caretaker(reason, payload or {}, force=False)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if getattr(channel, "guild", None) and not bot_config.SERVER_ADMIN_ENABLED:
            return
        self._invalidate_server_knowledge(
            "discord-channel-created",
            {"channel": getattr(channel, "name", ""), "channel_id": str(getattr(channel, "id", ""))},
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if getattr(channel, "guild", None) and not bot_config.SERVER_ADMIN_ENABLED:
            return
        self._invalidate_server_knowledge(
            "discord-channel-deleted",
            {"channel": getattr(channel, "name", ""), "channel_id": str(getattr(channel, "id", ""))},
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if getattr(after, "guild", None) and not bot_config.SERVER_ADMIN_ENABLED:
            return
        before_name = getattr(before, "name", "")
        after_name = getattr(after, "name", "")
        if before_name == after_name and getattr(before, "category_id", None) == getattr(after, "category_id", None):
            return
        self._invalidate_server_knowledge(
            "discord-channel-updated",
            {"before": before_name, "after": after_name, "channel_id": str(getattr(after, "id", ""))},
        )

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if not bot_config.SERVER_ADMIN_ENABLED:
            return
        self._invalidate_server_knowledge("discord-role-created", {"role": role.name, "role_id": str(role.id)})

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if not bot_config.SERVER_ADMIN_ENABLED:
            return
        self._invalidate_server_knowledge("discord-role-deleted", {"role": role.name, "role_id": str(role.id)})

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if not bot_config.SERVER_ADMIN_ENABLED:
            return
        if before.name == after.name and before.permissions == after.permissions and before.color == after.color:
            return
        self._invalidate_server_knowledge(
            "discord-role-updated",
            {"before": before.name, "after": after.name, "role_id": str(after.id)},
        )

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
