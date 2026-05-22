"""
Approval-gated AI operator actions.

The AI may propose maintenance actions, but this cog only executes a small
whitelist after an allowed owner approves the proposal in DM.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord.ext import commands

import config as bot_config
from config import COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING
from utils.ai_access import resolve_ai_operator_access
from utils.ai_caretaker import AICaretakerResult, sanitize_data, sanitize_text
from utils.attachments import (
    clear_recent_attachment_text,
    get_recent_attachment_text,
    read_message_attachments,
    store_attachment_text,
)

log = logging.getLogger(__name__)

ACTION_LABELS = {
    "run_r2_maintenance": "Run R2 maintenance",
    "run_steam_db_sync": "Run Steam DB sync",
    "run_ai_check": "Run AI caretaker check",
    "run_server_audit": "Run Discord server audit",
    "sync_booster_roles": "Sync Booster roles",
    "send_announcement": "Send announcement",
    "update_rules": "Update rules message",
    "pin_message": "Pin message",
    "set_channel_topic": "Set channel topic",
    "create_channel": "Create text channel",
    "configure_channel_access": "Configure channel access",
    "setup_channel_template": "Setup channel template",
    "create_role": "Create role",
    "update_role": "Update role",
    "delete_role": "Delete role",
    "timeout_member": "Timeout member",
    "kick_member": "Kick member",
    "ban_member": "Ban member",
    "create_webhook": "Create webhook",
    "delete_webhook": "Delete webhook",
    "update_server_settings": "Update server settings",
    "schedule_action": "Schedule operator action",
}

WRITE_ACTIONS = {
    "run_r2_maintenance",
    "run_steam_db_sync",
    "sync_booster_roles",
    "send_announcement",
    "update_rules",
    "pin_message",
    "set_channel_topic",
    "create_channel",
    "configure_channel_access",
    "setup_channel_template",
    "create_role",
    "update_role",
    "delete_role",
    "timeout_member",
    "kick_member",
    "ban_member",
    "create_webhook",
    "delete_webhook",
    "update_server_settings",
    "schedule_action",
}

SERVER_CONTENT_ACTIONS = {
    "send_announcement",
    "update_rules",
    "pin_message",
    "set_channel_topic",
    "create_channel",
    "configure_channel_access",
    "setup_channel_template",
    "create_role",
    "update_role",
    "delete_role",
    "timeout_member",
    "kick_member",
    "ban_member",
    "create_webhook",
    "delete_webhook",
    "update_server_settings",
}


@dataclass
class OperatorProposal:
    proposal_id: str
    action: str
    reason: str
    impact: str
    params: dict[str, Any] = field(default_factory=dict)
    source: str = "ai"
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    status: str = "pending"
    requested_by: Optional[int] = None
    approved_by: Optional[int] = None
    result: str = ""

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and time.time() > self.expires_at)


class AIOperator(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._pending: dict[str, OperatorProposal] = {}
        self._recent_signatures: dict[str, float] = {}
        self._drafts: dict[int, dict[str, Any]] = {}
        self._maintenance_lock = asyncio.Lock()
        self._server_lock = asyncio.Lock()
        self._ai_lock = asyncio.Lock()
        self._schedule_task: asyncio.Task | None = None
        bot.ai_operator = self

    async def cog_load(self):
        if bot_config.AI_OPERATOR_SCHEDULER_ENABLED:
            self._schedule_task = asyncio.create_task(self._schedule_loop())

    async def cog_unload(self):
        if self._schedule_task:
            self._schedule_task.cancel()
        if getattr(self.bot, "ai_operator", None) is self:
            self.bot.ai_operator = None

    def _is_owner(self, user_id: int) -> bool:
        """Backward-compatible explicit operator ID check."""
        return bool(bot_config.AI_OPERATOR_ALLOWED_IDS and user_id in bot_config.AI_OPERATOR_ALLOWED_IDS)

    async def _operator_access_for(self, user_id: int) -> tuple[bool, str, str]:
        return await resolve_ai_operator_access(self.bot, user_id)

    async def _is_operator_user(self, user_id: int) -> bool:
        allowed, _, _ = await self._operator_access_for(user_id)
        return allowed

    def _action_enabled(self, action: str) -> bool:
        if action == "run_r2_maintenance":
            return bool(bot_config.AI_OPERATOR_ALLOW_R2_MAINTENANCE)
        if action == "run_steam_db_sync":
            return bool(bot_config.AI_OPERATOR_ALLOW_STEAM_DB_SYNC)
        if action == "run_ai_check":
            return bool(bot_config.AI_OPERATOR_ALLOW_AI_RECHECK)
        if action == "run_server_audit":
            return bool(bot_config.AI_OPERATOR_ALLOW_SERVER_AUDIT)
        if action == "sync_booster_roles":
            return bool(bot_config.AI_OPERATOR_ALLOW_BOOSTER_SYNC)
        if action == "send_announcement":
            return bool(bot_config.AI_OPERATOR_ALLOW_SEND_ANNOUNCEMENT)
        if action == "update_rules":
            return bool(bot_config.AI_OPERATOR_ALLOW_UPDATE_RULES)
        if action == "pin_message":
            return bool(bot_config.AI_OPERATOR_ALLOW_PIN_MESSAGE)
        if action == "set_channel_topic":
            return bool(bot_config.AI_OPERATOR_ALLOW_SET_CHANNEL_TOPIC)
        if action == "create_channel":
            return bool(bot_config.AI_OPERATOR_ALLOW_CREATE_CHANNEL)
        if action == "configure_channel_access":
            return bool(bot_config.AI_OPERATOR_ALLOW_CONFIGURE_CHANNEL_ACCESS)
        if action == "setup_channel_template":
            return bool(bot_config.AI_OPERATOR_ALLOW_SETUP_CHANNEL_TEMPLATE)
        if action == "create_role":
            return bool(bot_config.AI_OPERATOR_ALLOW_CREATE_ROLE)
        if action == "update_role":
            return bool(bot_config.AI_OPERATOR_ALLOW_UPDATE_ROLE)
        if action == "delete_role":
            return bool(bot_config.AI_OPERATOR_ALLOW_DELETE_ROLE)
        if action == "timeout_member":
            return bool(bot_config.AI_OPERATOR_ALLOW_MEMBER_TIMEOUT)
        if action == "kick_member":
            return bool(bot_config.AI_OPERATOR_ALLOW_MEMBER_KICK)
        if action == "ban_member":
            return bool(bot_config.AI_OPERATOR_ALLOW_MEMBER_BAN)
        if action == "create_webhook":
            return bool(bot_config.AI_OPERATOR_ALLOW_WEBHOOK_CREATE)
        if action == "delete_webhook":
            return bool(bot_config.AI_OPERATOR_ALLOW_WEBHOOK_DELETE)
        if action == "update_server_settings":
            return bool(bot_config.AI_OPERATOR_ALLOW_SERVER_SETTING)
        if action == "schedule_action":
            return bool(bot_config.AI_OPERATOR_ALLOW_SCHEDULE_ACTION)
        return False

    def _cleanup(self) -> None:
        expired = [
            proposal_id
            for proposal_id, proposal in self._pending.items()
            if proposal.status != "pending" or proposal.expired
        ]
        for proposal_id in expired:
            self._pending.pop(proposal_id, None)

        cooldown = max(60, int(bot_config.AI_OPERATOR_PROPOSAL_COOLDOWN_SECONDS or 1800))
        old_signatures = [
            signature
            for signature, last_at in self._recent_signatures.items()
            if time.time() - last_at > cooldown
        ]
        for signature in old_signatures:
            self._recent_signatures.pop(signature, None)

    def _lock_for_action(self, action: str) -> tuple[asyncio.Lock, str]:
        if action in {"run_r2_maintenance", "run_steam_db_sync"}:
            return self._maintenance_lock, "maintenance"
        if action in {"run_server_audit", "sync_booster_roles"} or action in SERVER_CONTENT_ACTIONS:
            return self._server_lock, "server administration"
        return self._ai_lock, "AI caretaker"

    def _parse_operator_command(self, text: str) -> tuple[str, Optional[str]]:
        clean = sanitize_text(text).strip()
        lower = re.sub(r"\s+", " ", clean.lower()).strip(" `.,!;:")
        approve_words = r"approve|approved|accept|accepted|acc|prove|aprove|aproove|approv|approvee|setuju|yes|ya|oke|ok|lanjut|lanjutkan|continue|proceed|confirm|konfirmasi|jalankan|jalanin|gas"
        reject_words = r"reject|rejected|deny|cancel|tolak|batal|no"
        all_words = r"all(?:\s+of\s+them)?|semua|semuanya|all\s+proposals?|all\s+approval(?:s)?"
        latest_words = r"latest|last|terbaru|terakhir"
        id_prefix = r"(?:(?:proposal|proposal id|id)\s+)?"

        match = re.fullmatch(rf"(?:{approve_words})\s+{id_prefix}([a-f0-9]{{6}})", lower)
        if match:
            return ("approve", match.group(1))
        if re.fullmatch(rf"(?:{approve_words})\s+(?:{latest_words})|(?:{latest_words})\s+(?:{approve_words})", lower):
            return ("approve", "latest")
        match = re.fullmatch(rf"([a-f0-9]{{6}})\s+(?:{approve_words})", lower)
        if match:
            return ("approve", match.group(1))
        if re.fullmatch(rf"(?:{all_words})", lower):
            return ("approve_all", None)
        if re.fullmatch(
            rf"(?:(?:{all_words})\s+)?(?:{approve_words})\s+(?:{all_words})",
            lower,
        ) or re.fullmatch(
            rf"(?:{all_words})\s+(?:{approve_words})",
            lower,
        ):
            return ("approve_all", None)
        if re.fullmatch(
            rf"(?:{approve_words})",
            lower,
        ):
            return ("approve", None)

        match = re.fullmatch(rf"(?:{reject_words})\s+{id_prefix}([a-f0-9]{{6}})", lower)
        if match:
            return ("reject", match.group(1))
        if re.fullmatch(rf"(?:{reject_words})\s+(?:{latest_words})|(?:{latest_words})\s+(?:{reject_words})", lower):
            return ("reject", "latest")
        match = re.fullmatch(rf"([a-f0-9]{{6}})\s+(?:{reject_words})", lower)
        if match:
            return ("reject", match.group(1))
        if re.fullmatch(
            rf"(?:{reject_words})\s+(?:{all_words})|(?:{all_words})\s+(?:{reject_words})",
            lower,
        ):
            return ("reject_all", None)
        if re.fullmatch(rf"(?:{reject_words})", lower):
            return ("reject", None)

        if re.fullmatch(
            r"(?:pending|list|show|daftar|lihat|cek)\s+(?:approval|approvals|proposal|proposals)|approval pending|pending approval|approval",
            lower,
        ):
            return ("pending", None)
        return ("", None)

    def is_operator_control_text(self, text: str) -> bool:
        command, _ = self._parse_operator_command(text)
        return bool(command)

    @staticmethod
    def _strip_outer_quotes(value: str) -> str:
        text = value.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1].strip()
        return text

    def _strip_value_prefix(self, text: str, *, field: str | None = None) -> str:
        clean = sanitize_text(text).strip()
        if not clean:
            return ""

        patterns: list[str]
        if field == "name":
            patterns = [
                r"^(?:nama\s*(?:nya)?|namanya|name(?:\s+is)?|channel\s+name)\s*[:=\-]?\s*",
            ]
        elif field == "topic":
            patterns = [
                r"^(?:topic|topik)(?:\s*(?:nya|is))?\s*[:=\-]?\s*",
            ]
        else:
            patterns = [
                r"^(?:kirim|send|post|isi|content|konten)\s*[:=\-]?\s*",
                r"^(?:topic|topik)(?:\s*(?:nya|is))?\s*[:=\-]?\s*",
                r"^(?:nama\s*(?:nya)?|namanya|name(?:\s+is)?|channel\s+name)\s*[:=\-]?\s*",
            ]

        for pattern in patterns:
            clean = re.sub(pattern, "", clean, flags=re.I).strip()
        clean = re.sub(r"\s+(?:aja|saja|please|pls)$", "", clean, flags=re.I).strip()
        return self._strip_outer_quotes(clean)

    @staticmethod
    def _looks_like_channel_purpose(value: str) -> bool:
        lower = sanitize_text(value).strip().lower()
        if not lower:
            return False
        if re.match(r"^(?:untuk|buat|for|to)\b", lower):
            return True
        return any(
            phrase in lower
            for phrase in (
                "menyambut",
                "orang baru",
                "baru join",
                "new member",
                "new members",
                "welcome member",
                "welcome members",
                "member baru",
            )
        )

    def _extract_channel_and_content(self, text: str) -> tuple[str, str]:
        clean = sanitize_text(text).strip()
        channel = ""
        content = ""
        explicit_channel = re.search(r"(<#\d+>|#[\w-]+)", clean)
        if explicit_channel:
            channel = explicit_channel.group(1).strip()
            content = clean[explicit_channel.end() :].strip(" :-?")
            return channel, self._strip_outer_quotes(content)

        match = re.search(r"(?:di|to|ke|in)\s+(<#\d+>|#[\w-]+|[\w-]+)\s*[:\-]\s*(.+)\Z", clean, re.I | re.S)
        if match:
            return match.group(1).strip(), self._strip_outer_quotes(match.group(2))
        match = re.search(r"(?:di|to|ke|in)\s+(<#\d+>|#[\w-]+|[\w-]+)", clean, re.I)
        if match:
            channel = match.group(1).strip()
            content = clean[match.end() :].strip(" :-")
        else:
            match = re.search(r"(<#\d+>|#[\w-]+)", clean)
            if match:
                channel = match.group(1).strip()
                content = clean[match.end() :].strip(" :-")
        return channel, self._strip_outer_quotes(content)

    @staticmethod
    def _extract_channel_reference(text: str) -> str:
        clean = sanitize_text(text).strip()
        match = re.search(r"(?:channel\s*)?id\s*[:=\-]?\s*(\d{16,25})", clean, re.I)
        if match:
            return f"<#{match.group(1)}>"
        match = re.search(r"(<#\d+>|#[\w-]+)", clean)
        if match:
            return match.group(1).strip()
        match = re.search(
            r"(?:channel|kanal|saluran)\s+([#]?[a-z0-9][a-z0-9_-]{1,90})",
            clean,
            re.I,
        )
        if match:
            value = match.group(1).strip()
            return value if value.startswith("#") else f"#{value}"
        return ""

    def _parse_channel_access_request(self, text: str) -> tuple[Optional[str], dict[str, Any]]:
        clean = sanitize_text(text).strip()
        lower = clean.lower()
        if not clean:
            return None, {}

        access_intent = any(
            phrase in lower
            for phrase in (
                "permission",
                "permissions",
                "akses",
                "access",
                "has permission",
                "permission to send",
                "bot has permission",
                "triadbot has permission",
                "bot permission",
                "bot permissions",
                "izin bot",
                "bot bisa",
                "triadbot bisa",
                "bisa chat",
                "bisa kirim",
                "kirim pesan",
                "send message",
                "send messages",
                "chat only",
                "only chat",
                "read only",
                "read-only",
                "lock channel",
                "kunci channel",
                "hanya admin",
                "cuma admin",
                "admin dan moderator",
                "admin/mod",
                "moderator yang bisa",
                "mod yang bisa",
                "staff only",
                "only staff",
                "only admin",
            )
        )
        if not access_intent:
            return None, {}

        channel = self._extract_channel_reference(clean)
        channel_context = bool(channel) or bool(
            re.search(r"\b(?:atur|set|ubah|configure|lock|kunci)\s+(?:channel|kanal|saluran)\b", lower)
        )
        if not channel_context:
            return None, {}

        send_intent = any(
            phrase in lower
            for phrase in (
                "chat",
                "send",
                "kirim",
                "pesan",
                "bicara",
                "ngobrol",
                "write",
                "message",
                "messages",
            )
        )
        staff_send_intent = any(
            phrase in lower
            for phrase in (
                "hanya admin",
                "cuma admin",
                "only admin",
                "admin dan moderator",
                "admin/mod",
                "moderator yang bisa",
                "mod yang bisa",
                "staff only",
                "only staff",
            )
        ) and send_intent
        bot_send_intent = any(
            phrase in lower
            for phrase in (
                "bot",
                "triadbot",
                "bot role",
                "role bot",
                "peran bot",
                "my role",
            )
        ) and send_intent and any(
            phrase in lower
            for phrase in (
                "permission",
                "permissions",
                "izin",
                "access",
                "akses",
                "can",
                "bisa",
            )
        )
        if not (staff_send_intent or bot_send_intent):
            return None, {}

        return (
            "configure_channel_access",
            {
                "channel": channel,
                "mode": "admin_mod_only_send",
                "reason": clean[:500],
            },
        )

    @staticmethod
    def _is_read_only_rules_request(text: str) -> bool:
        clean = sanitize_text(text).strip()
        lower = clean.lower()
        if not clean or not re.search(r"\brules?\b|\bperaturan\b", lower):
            return False

        write_intent = any(
            phrase in lower
            for phrase in (
                "buat rules",
                "buat peraturan",
                "bikin rules",
                "bikin peraturan",
                "make rules",
                "make it rules",
                "create rules",
                "update rules",
                "update peraturan",
                "ubah rules",
                "ubah peraturan",
                "ganti rules",
                "ganti peraturan",
                "post rules",
                "post peraturan",
                "kirim rules",
                "kirim peraturan",
                "pasang rules",
                "pasang peraturan",
                "copy the text and make it rules",
                "salin teks",
                "ambil teks",
            )
        )
        if write_intent:
            return False

        read_intent = any(
            phrase in lower
            for phrase in (
                "jelaskan",
                "explain",
                "apa",
                "what",
                "how",
                "gimana",
                "bagaimana",
                "tampilkan",
                "show",
                "lihat",
                "cek",
                "check",
                "list",
                "daftar",
                "rangkum",
                "summarize",
                "sebutkan",
                "kasih tahu",
                "beri tahu",
                "tell me",
            )
        )
        if read_intent or "?" in clean:
            return True

        normalized = re.sub(r"[^a-z0-9\s]", " ", lower)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized in {"rules", "server rules", "rules server", "peraturan", "peraturan server"}

    def _parse_server_content_request(self, text: str) -> tuple[Optional[str], dict[str, Any]]:
        clean = sanitize_text(text).strip()
        lower = clean.lower()
        if not clean:
            return None, {}

        access_action, access_params = self._parse_channel_access_request(clean)
        if access_action:
            return access_action, access_params

        if re.search(r"(?:buat|create|bikin|add)\s+(?:text\s+)?channel", lower):
            name = ""
            topic = ""
            name_match = re.search(
                r"(?:nama\s*(?:nya)?|namanya|name(?:\s+is)?|channel\s+name)\s*[:=\-]?\s*(.+)\Z",
                clean,
                re.I | re.S,
            )
            if name_match:
                name = self._strip_value_prefix(name_match.group(1), field="name")
            match = re.search(
                r"(?:channel)\s+(?:baru\s+)?[#`'\"]?(.{2,100}?)(?:\s+(?:topic|topik)\b|$)",
                clean,
                re.I,
            )
            if match and not name:
                name = match.group(1).strip(" `\"'")
            topic_match = re.search(r"(?:topic|topik)\s*[:\-]\s*(.+)\Z", clean, re.I | re.S)
            if topic_match:
                topic = self._strip_outer_quotes(topic_match.group(1))
            if self._looks_like_channel_purpose(name):
                topic = topic or name
                name = ""
            return "create_channel", {"name": name, "topic": topic}

        if re.search(r"\b(?:set|atur|ubah|update|ganti)\s+(?:channel\s+)?(?:topic|topik)\b", lower):
            channel, topic = self._extract_channel_and_content(clean)
            return "set_channel_topic", {"channel": channel, "topic": topic}

        if "pin" in lower:
            channel_match = re.search(r"(<#\d+>|#[\w-]+)", clean)
            channel = channel_match.group(1).strip() if channel_match else ""
            if not channel:
                channel, _ = self._extract_channel_and_content(clean)
            message_id = ""
            match = re.search(r"\b(\d{16,25})\b", clean)
            if match:
                message_id = match.group(1)
            return "pin_message", {"channel": channel, "message_id": message_id}

        if any(word in lower for word in ("announce", "announcement", "pengumuman")):
            channel, content = self._extract_channel_and_content(clean)
            if not content and not channel:
                match = re.search(
                    r"\b(?:announcement|announce|pengumuman|kirim|send|post)(?:kan)?(?:\s+lagi)?\s*(?:pesan)?\s*(?:[:\-]\s*)?(.+)\Z",
                    clean,
                    re.I | re.S,
                )
                content = self._strip_outer_quotes(match.group(1)) if match else ""
            return "send_announcement", {"channel": channel, "content": content, "title": "Announcement"}

        if re.search(r"\brules?\b|\bperaturan\b", lower):
            if self._is_read_only_rules_request(clean):
                return None, {}
            channel, content = self._extract_channel_and_content(clean)
            if not content and not channel:
                match = re.search(r"(?:rules?|peraturan)(?:\s+server)?\s*[:\-]\s*(.+)\Z", clean, re.I | re.S)
                content = self._strip_outer_quotes(match.group(1)) if match else ""
            return "update_rules", {"channel": channel, "content": content, "title": "Server Rules", "pin": True}

        if any(phrase in lower for phrase in ("template", "setup category", "setup kategori", "kategori game", "game category", "game channels")):
            if any(word in lower for word in ("buat", "bikin", "create", "setup", "siapkan", "atur")):
                category = ""
                template = "game"
                if "support" in lower:
                    template = "support"
                elif "community" in lower or "server" in lower:
                    template = "community"
                match = re.search(r"(?:category|kategori)\s+[#`'\"]?(.{2,100}?)(?:\s+(?:template|dengan|with|$)|$)", clean, re.I)
                if match:
                    category = match.group(1).strip(" `\"'")
                if not category:
                    match = re.search(r"(?:template|setup)\s+[#`'\"]?(.{2,100})", clean, re.I)
                    if match:
                        category = match.group(1).strip(" `\"'")
                return "setup_channel_template", {"template": template, "category": category or "Games"}

        role_match = re.search(r"(?:buat|bikin|create|add)\s+role\s+@?([\w\- ]{2,80})", clean, re.I)
        if role_match:
            color_match = re.search(r"(?:color|warna)\s*[:=]?\s*(#[0-9a-fA-F]{6}|[a-zA-Z]+)", clean)
            return "create_role", {"name": role_match.group(1).strip(), "color": color_match.group(1) if color_match else ""}

        role_update_match = re.search(r"(?:ubah|update|edit)\s+role\s+@?([\w\- ]{2,80})", clean, re.I)
        if role_update_match:
            new_name_match = re.search(r"(?:jadi|to|new name|nama baru)\s+@?([\w\- ]{2,80})", clean, re.I)
            color_match = re.search(r"(?:color|warna)\s*[:=]?\s*(#[0-9a-fA-F]{6}|[a-zA-Z]+)", clean)
            return "update_role", {
                "role": role_update_match.group(1).strip(),
                "new_name": new_name_match.group(1).strip() if new_name_match else "",
                "color": color_match.group(1) if color_match else "",
            }

        role_delete_match = re.search(r"(?:hapus|delete|remove)\s+role\s+@?([\w\- ]{2,80})", clean, re.I)
        if role_delete_match:
            return "delete_role", {"role": role_delete_match.group(1).strip()}

        if re.search(r"\btimeout\b|\bmute\b", lower):
            member_match = re.search(r"(<@!?\d+>|\b\d{16,25}\b|@[^\s]+)", clean)
            duration_match = re.search(r"\b(\d+\s*(?:s|sec|secs|second|seconds|m|min|minute|minutes|h|hour|hours|d|day|days))\b", clean, re.I)
            return "timeout_member", {
                "member": member_match.group(1) if member_match else "",
                "duration": duration_match.group(1) if duration_match else "10m",
                "reason": clean[:500],
            }

        if re.search(r"\bkick\b", lower):
            member_match = re.search(r"(<@!?\d+>|\b\d{16,25}\b|@[^\s]+)", clean)
            return "kick_member", {"member": member_match.group(1) if member_match else "", "reason": clean[:500]}

        if re.search(r"\bban\b", lower):
            member_match = re.search(r"(<@!?\d+>|\b\d{16,25}\b|@[^\s]+)", clean)
            return "ban_member", {"member": member_match.group(1) if member_match else "", "reason": clean[:500]}

        webhook_create = re.search(r"(?:buat|create|bikin)\s+webhook", clean, re.I)
        if webhook_create:
            channel = self._extract_channel_reference(clean)
            name_match = re.search(r"(?:nama|name)\s*[:=]?\s*([\w\- ]{2,80})", clean, re.I)
            return "create_webhook", {"channel": channel, "name": name_match.group(1).strip() if name_match else "TriadBot Webhook"}

        webhook_delete = re.search(r"(?:hapus|delete|remove)\s+webhook\s+(\d{16,25})", clean, re.I)
        if webhook_delete:
            return "delete_webhook", {"webhook_id": webhook_delete.group(1)}

        if any(phrase in lower for phrase in ("nama server", "server name", "description server", "deskripsi server")):
            if any(word in lower for word in ("ubah", "update", "set", "ganti")):
                name_match = re.search(r"(?:nama server|server name)\s*(?:jadi|to|[:=])\s*(.{2,100})", clean, re.I)
                desc_match = re.search(r"(?:description server|deskripsi server|server description)\s*(?:jadi|to|[:=])\s*(.{2,120})", clean, re.I)
                return "update_server_settings", {
                    "name": name_match.group(1).strip() if name_match else "",
                    "description": desc_match.group(1).strip() if desc_match else "",
                }

        if any(word in lower for word in ("jadwal", "schedule", "scheduled", "otomatis", "setiap", "tiap")) and any(
            word in lower for word in ("maintenance", "sync", "audit", "booster", "caretaker")
        ):
            return "schedule_action", {"schedule_text": clean}

        return None, {}

    def _parse_action_request(self, text: str) -> Optional[str]:
        lower = sanitize_text(text).lower()
        has_run_intent = any(
            phrase in lower
            for phrase in (
                "buat proposal",
                "create proposal",
                "request approval",
                "minta approval",
                "jalankan",
                "run ",
                "start ",
                "mulai",
                "execute",
            )
        )
        if not has_run_intent:
            return None
        if "r2" in lower and "maintenance" in lower:
            return "run_r2_maintenance"
        if "steam" in lower and ("sync" in lower or "database" in lower or "db" in lower):
            return "run_steam_db_sync"
        if ("ai" in lower or "caretaker" in lower) and ("check" in lower or "cek" in lower):
            return "run_ai_check"
        if ("server" in lower or "discord" in lower) and ("audit" in lower or "cek" in lower or "check" in lower):
            return "run_server_audit"
        if "booster" in lower and ("sync" in lower or "sinkron" in lower or "rapikan" in lower):
            return "sync_booster_roles"
        action, _ = self._parse_server_content_request(text)
        if action:
            return action
        return None

    def _single_pending_for_user(
        self,
        user_id: int,
        *,
        action: str | None = None,
    ) -> Optional[OperatorProposal]:
        self._cleanup()
        pending = [
            item
            for item in self._pending.values()
            if item.status == "pending"
            and not item.expired
            and item.requested_by == user_id
            and (not action or item.action == action)
        ]
        if len(pending) == 1:
            return pending[0]
        return None

    def _parse_pending_update(
        self,
        text: str,
        user_id: int,
    ) -> tuple[Optional[OperatorProposal], dict[str, Any]]:
        clean = sanitize_text(text).strip()
        if not clean:
            return None, {}

        lower = clean.lower()
        explicit_channel_update = bool(
            re.match(
                r"^(?:nama\s*(?:nya)?|namanya|name(?:\s+is)?|channel\s+name|topic|topik)\b",
                clean,
                re.I,
            )
        )
        proposal = self._single_pending_for_user(user_id)
        if not proposal and explicit_channel_update:
            proposal = self._single_pending_for_user(user_id, action="create_channel")
        if not proposal:
            return None, {}

        if proposal.action == "create_channel":
            name_match = re.match(
                r"^(?:nama\s*(?:nya)?|namanya|name(?:\s+is)?|channel\s+name)\s*[:=\-]?\s*(.+)\Z",
                clean,
                re.I | re.S,
            )
            if name_match:
                name = self._strip_value_prefix(name_match.group(1), field="name")
                return proposal, {"name": name} if name else {}

            topic_match = re.match(r"^(?:topic|topik)(?:\s*(?:nya|is))?\s*[:=\-]?\s*(.+)\Z", clean, re.I | re.S)
            if topic_match:
                topic = self._strip_value_prefix(topic_match.group(1), field="topic")
                return proposal, {"topic": topic} if topic else {}

            if (
                len(clean) <= 100
                and len(clean.split()) <= 5
                and not any(word in lower for word in ("buat", "create", "bikin", "announce", "rules", "approve", "reject"))
            ):
                return proposal, {"name": self._strip_outer_quotes(clean)}

        return None, {}

    def is_operator_command(self, text: str, user_id: int) -> bool:
        # Synchronous fast path used by AIChat. It can only verify explicit IDs.
        # Role-based operator access is handled by is_operator_command_for_user().
        if not bot_config.AI_OPERATOR_ENABLED or not self._is_owner(user_id):
            return False
        if self.is_operator_control_text(text):
            return True
        if user_id in self._drafts:
            return True
        proposal, updates = self._parse_pending_update(text, user_id)
        if proposal and updates:
            return True
        action, _ = self._parse_server_content_request(text)
        return bool(action or self._parse_action_request(text))

    async def is_operator_command_for_user(self, text: str, user_id: int) -> bool:
        if not bot_config.AI_OPERATOR_ENABLED or not await self._is_operator_user(user_id):
            return False
        if self.is_operator_control_text(text):
            return True
        if user_id in self._drafts:
            return True
        proposal, updates = self._parse_pending_update(text, user_id)
        if proposal and updates:
            return True
        action, _ = self._parse_server_content_request(text)
        return bool(action or self._parse_action_request(text))

    async def propose_from_ai_result(self, result: AICaretakerResult, *, reason: str) -> None:
        if not bot_config.AI_OPERATOR_ENABLED:
            return
        for item in result.proposed_actions:
            action = str(item.get("action", "")).strip().lower()
            if action == "run_ai_check":
                continue
            await self.create_proposal(
                action=action,
                reason=item.get("reason") or result.summary or f"AI caretaker triggered by {reason}",
                impact=item.get("impact") or "This action will use the current your .env file configuration.",
                params=item.get("params") if isinstance(item.get("params"), dict) else {},
                source=f"ai-caretaker:{reason}",
                dedupe=True,
            )

    async def create_proposal(
        self,
        *,
        action: str,
        reason: str,
        impact: str,
        params: Optional[dict[str, Any]] = None,
        source: str = "ai",
        requested_by: Optional[int] = None,
        dedupe: bool = True,
    ) -> Optional[OperatorProposal]:
        if not bot_config.AI_OPERATOR_ENABLED:
            return None
        action = sanitize_text(action).strip().lower()
        if action not in ACTION_LABELS or not self._action_enabled(action):
            return None

        self._cleanup()
        safe_params = sanitize_data(params or {})
        signature = f"{action}:{json.dumps(safe_params, sort_keys=True, ensure_ascii=True)}"
        cooldown = max(0, int(bot_config.AI_OPERATOR_PROPOSAL_COOLDOWN_SECONDS or 0))
        if dedupe and cooldown and time.time() - self._recent_signatures.get(signature, 0) < cooldown:
            return None

        while len(self._pending) >= max(1, int(bot_config.AI_OPERATOR_MAX_PENDING or 10)):
            oldest_id = min(self._pending, key=lambda item: self._pending[item].created_at)
            self._pending.pop(oldest_id, None)

        proposal_id = secrets.token_hex(3)
        proposal = OperatorProposal(
            proposal_id=proposal_id,
            action=action,
            reason=sanitize_text(reason)[:700] or "Operational issue detected.",
            impact=sanitize_text(impact)[:700] or "This action will use the current safe configuration.",
            params=safe_params,
            source=sanitize_text(source)[:120],
            expires_at=time.time() + max(60, int(bot_config.AI_OPERATOR_APPROVAL_TTL_SECONDS or 900)),
            requested_by=requested_by,
        )
        self._pending[proposal_id] = proposal
        self._recent_signatures[signature] = time.time()
        await self._notify_owners(proposal)
        return proposal

    def _proposal_embed(self, proposal: OperatorProposal) -> discord.Embed:
        embed = discord.Embed(
            title="Approval required",
            description=(
                "I prepared a whitelisted action. I will not change anything until it is approved."
            ),
            color=COLOR_WARNING,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Proposal ID", value=f"`{proposal.proposal_id}`", inline=True)
        embed.add_field(name="Action", value=ACTION_LABELS[proposal.action], inline=True)
        embed.add_field(name="Source", value=f"`{proposal.source}`", inline=True)
        embed.add_field(name="Reason", value=proposal.reason[:1024], inline=False)
        embed.add_field(name="Impact", value=proposal.impact[:1024], inline=False)
        if proposal.params:
            params = json.dumps(proposal.params, ensure_ascii=False, sort_keys=True)
            embed.add_field(name="Parameters", value=f"`{params[:900]}`", inline=False)
        embed.add_field(
            name="Approve",
            value=f"Reply `approve {proposal.proposal_id}`",
            inline=True,
        )
        embed.add_field(
            name="Reject",
            value=f"Reply `reject {proposal.proposal_id}`",
            inline=True,
        )
        embed.add_field(
            name="Expires",
            value=f"in {int(max(0, proposal.expires_at - time.time()))} seconds",
            inline=True,
        )
        return embed

    async def _notify_owners(self, proposal: OperatorProposal) -> None:
        for user_id in bot_config.AI_OPERATOR_ALLOWED_IDS:
            try:
                user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
                await user.send(embed=self._proposal_embed(proposal))
            except Exception:
                log.warning("Could not send AI operator proposal to owner %s", user_id, exc_info=True)

    def _pending_proposals(self) -> list[OperatorProposal]:
        self._cleanup()
        return sorted(
            (
                proposal
                for proposal in self._pending.values()
                if proposal.status == "pending" and not proposal.expired
            ),
            key=lambda item: item.created_at,
        )

    def _pending_lines(self, proposals: list[OperatorProposal]) -> list[str]:
        lines: list[str] = []
        visible = proposals[:15]
        for proposal in visible:
            ttl = int(max(0, proposal.expires_at - time.time()))
            lines.append(f"`{proposal.proposal_id}` - {ACTION_LABELS[proposal.action]} - expires in {ttl}s")
        remaining = len(proposals) - len(visible)
        if remaining > 0:
            lines.append(f"... and {remaining} more pending proposal(s).")
        return lines

    async def _send_missing_proposal_id(
        self,
        channel: discord.abc.Messageable,
        *,
        verb: str,
    ) -> None:
        pending = self._pending_proposals()
        if not pending:
            await channel.send(
                "No pending approvals right now. A real proposal always appears as an "
                "`Approval required` card with a `Proposal ID`. Ask me for a supported "
                f"action first, then reply `{verb} <id>` using the ID shown on that card."
            )
            return
        if len(pending) == 1:
            proposal = pending[0]
            await channel.send(
                f"One proposal is pending: `{proposal.proposal_id}` - {ACTION_LABELS[proposal.action]}. "
                f"Reply `{verb} {proposal.proposal_id}` to continue."
            )
            return
        await channel.send(
            f"Multiple proposals are pending. Reply `{verb} <id>` for one of these, "
            f"or reply `{verb} all` to apply every pending proposal:\n"
            + "\n".join(self._pending_lines(pending))
        )

    async def _send_pending(self, channel: discord.abc.Messageable) -> None:
        pending = self._pending_proposals()
        if not pending:
            await channel.send("No pending approvals right now.")
            return
        await channel.send("Pending approvals:\n" + "\n".join(self._pending_lines(pending)))

    def _resolve_single_pending_id(self, proposal_id: Optional[str]) -> Optional[str]:
        if proposal_id and proposal_id != "latest":
            return proposal_id
        pending = [item.proposal_id for item in self._pending_proposals()]
        if proposal_id == "latest" and pending:
            return pending[-1]
        if len(pending) == 1:
            return pending[0]
        return None

    def _recent_ai_chat_items(self, user_id: int, *, limit: int = 10) -> list[dict[str, str]]:
        chat = self.bot.get_cog("AIChat")
        memory = getattr(chat, "memory", None)
        if not memory or not hasattr(memory, "snapshot"):
            return []
        try:
            raw_items = list(memory.snapshot(user_id))[-limit:]
        except Exception:
            log.debug("Could not read AI chat memory for contextual approval", exc_info=True)
            return []

        items: list[dict[str, str]] = []
        for item in raw_items:
            role = sanitize_text(str(item.get("role") or "")).strip().lower()
            text = sanitize_text(str(item.get("text") or "")).strip()
            if role and text:
                items.append({"role": role, "text": text[:2000]})
        return items

    def _recent_owner_user_context(self, user_id: int) -> str:
        items = self._recent_ai_chat_items(user_id, limit=12)
        user_lines: list[str] = []
        for item in items:
            if item.get("role") != "user":
                continue
            text = item.get("text") or ""
            command, _ = self._parse_operator_command(text)
            if command in {"approve", "approve_all", "reject", "reject_all", "pending"}:
                continue
            user_lines.append(text)
        return "\n".join(user_lines[-5:]).strip()

    def _extract_followup_channel(self, text: str) -> str:
        channel = self._extract_channel_reference(text)
        if channel:
            return channel

        clean = sanitize_text(text)
        match = re.search(r"(?:channel\s*)?id\s*[:=\-]?\s*(\d{16,25})", clean, re.I)
        if match:
            return f"<#{match.group(1)}>"

        lower = clean.lower()
        if "welcome" in lower or "selamat datang" in lower or "menyambut" in lower:
            return "#welcome"
        if "announcement" in lower or "announce" in lower or "pengumuman" in lower:
            return "#announcement"
        if "rules" in lower or "peraturan" in lower:
            return "#rules"
        return ""

    def _contextual_followup_actions(
        self,
        user_id: int,
    ) -> tuple[list[tuple[str, dict[str, Any], str, str]], list[str]]:
        context = self._recent_owner_user_context(user_id)
        if not context:
            return [], []

        lower = context.lower()
        actions: list[tuple[str, dict[str, Any], str, str]] = []
        notes: list[str] = []

        channel = self._extract_followup_channel(context)
        staff_words = any(
            phrase in lower
            for phrase in (
                "admin",
                "moderator",
                "mod",
                "staff",
                "bot role",
                "role bot",
                "my role",
                "peran bot",
                "triadbot",
                "triadbot role",
                "role triadbot",
            )
        )
        send_words = any(
            phrase in lower
            for phrase in (
                "chat",
                "send",
                "kirim",
                "pesan",
                "bicara",
                "ngobrol",
                "write",
            )
        )
        access_words = any(
            phrase in lower
            for phrase in (
                "permission",
                "permissions",
                "akses",
                "access",
                "only",
                "hanya",
                "cuma",
                "lock",
                "kunci",
                "pastikan izin",
                "ensure permission",
            )
        )
        if (
            channel
            and staff_words
            and send_words
            and access_words
            and self._action_enabled("configure_channel_access")
        ):
            actions.append(
                (
                    "configure_channel_access",
                    {
                        "channel": channel,
                        "mode": "admin_mod_only_send",
                        "reason": context[-500:],
                    },
                    "Follow-up from the previous owner chat: configure channel access.",
                    "This changes channel overwrites so everyone can read, but only configured staff roles and the bot can send messages.",
                )
            )

        if "r2" in lower and "maintenance" in lower and self._action_enabled("run_r2_maintenance"):
            actions.append(
                (
                    "run_r2_maintenance",
                    {},
                    "Follow-up from the previous owner chat: run R2 maintenance.",
                    "Safe maintenance run using current R2 rules. It can rename/clean objects only within the configured maintenance scope.",
                )
            )

        if "steam" in lower and ("sync" in lower or "database" in lower or "db" in lower) and self._action_enabled(
            "run_steam_db_sync"
        ):
            actions.append(
                (
                    "run_steam_db_sync",
                    {},
                    "Follow-up from the previous owner chat: run Steam DB sync.",
                    "Reads Steam catalog data and updates the local games database according to the configured sync rules.",
                )
            )

        if ("server" in lower or "discord" in lower) and ("audit" in lower or "cek" in lower or "check" in lower):
            if self._action_enabled("run_server_audit"):
                actions.append(
                    (
                        "run_server_audit",
                        {},
                        "Follow-up from the previous owner chat: run Discord server audit.",
                        "Read-only server audit. It checks configured roles, permissions, channels, and Booster role state.",
                    )
                )

        if "welcome-message" in lower or "welcome message" in lower or "pesan selamat datang" in lower:
            notes.append(
                "Welcome-message automation is not a whitelisted executable action yet, so I can only propose channel access changes for now."
            )
        if any(phrase in lower for phrase in ("hapus", "delete", "clear", "bersihkan")) and any(
            phrase in lower for phrase in ("history", "message", "pesan", "gallery")
        ):
            notes.append("Bulk deleting old channel messages is not whitelisted, so I skipped that part.")

        return actions[:3], notes[:3]

    async def _create_contextual_followup_proposals(
        self,
        user_id: int,
        channel: discord.abc.Messageable,
    ) -> bool:
        actions, notes = self._contextual_followup_actions(user_id)
        if not actions:
            return False

        created: list[OperatorProposal] = []
        for action, params, reason, impact in actions:
            proposal = await self.create_proposal(
                action=action,
                reason=reason,
                impact=impact,
                params=params,
                source="owner-followup",
                requested_by=user_id,
                dedupe=False,
            )
            if proposal:
                created.append(proposal)

        if not created:
            return False

        lines = [
            "Saya ubah konteks chat terakhir menjadi proposal resmi:",
            *[
                f"`{proposal.proposal_id}` - {ACTION_LABELS[proposal.action]}"
                for proposal in created
            ],
        ]
        if notes:
            lines.append("Catatan:")
            lines.extend(f"- {note}" for note in notes)
        lines.append("Balas `approve <id>` untuk menjalankan proposal yang dipilih.")
        await channel.send("\n".join(lines))
        return True

    async def _reject(self, proposal_id: Optional[str], user_id: int, channel: discord.abc.Messageable) -> None:
        proposal_id = self._resolve_single_pending_id(proposal_id)
        if not proposal_id:
            await self._send_missing_proposal_id(channel, verb="reject")
            return
        proposal = self._pending.pop(proposal_id, None)
        if not proposal or proposal.expired:
            await channel.send(f"Proposal `{proposal_id}` is not pending or has expired.")
            return
        proposal.status = "rejected"
        proposal.approved_by = user_id
        await channel.send(f"Rejected proposal `{proposal_id}`. No changes were made.")

    async def _reject_all(self, user_id: int, channel: discord.abc.Messageable) -> None:
        pending = self._pending_proposals()
        if not pending:
            await self._send_missing_proposal_id(channel, verb="reject")
            return
        ids = [proposal.proposal_id for proposal in pending]
        for proposal_id in ids:
            proposal = self._pending.pop(proposal_id, None)
            if proposal and not proposal.expired:
                proposal.status = "rejected"
                proposal.approved_by = user_id
        await channel.send(
            "Rejected all pending proposals. No changes were made: "
            + ", ".join(f"`{proposal_id}`" for proposal_id in ids)
        )

    async def _approve_all(self, user_id: int, channel: discord.abc.Messageable) -> None:
        pending = self._pending_proposals()
        if not pending:
            if await self._create_contextual_followup_proposals(user_id, channel):
                return
            await self._send_missing_proposal_id(channel, verb="approve")
            return
        ids = [proposal.proposal_id for proposal in pending]
        await channel.send(
            "Approving all pending proposals in order: "
            + ", ".join(f"`{proposal_id}`" for proposal_id in ids)
        )
        for proposal_id in ids:
            if proposal_id in self._pending:
                await self._approve(proposal_id, user_id, channel)
                await asyncio.sleep(0.2)

    async def _approve(self, proposal_id: Optional[str], user_id: int, channel: discord.abc.Messageable) -> None:
        proposal_id = self._resolve_single_pending_id(proposal_id)
        if not proposal_id:
            if await self._create_contextual_followup_proposals(user_id, channel):
                return
            await self._send_missing_proposal_id(channel, verb="approve")
            return
        proposal = self._pending.get(proposal_id)
        if not proposal or proposal.expired:
            self._pending.pop(proposal_id, None)
            await channel.send(f"Proposal `{proposal_id}` is not pending or has expired.")
            return
        if proposal.status != "pending":
            await channel.send(f"Proposal `{proposal_id}` is already `{proposal.status}`.")
            return
        lock, lock_name = self._lock_for_action(proposal.action)
        if lock.locked():
            await channel.send(f"Another {lock_name} action is already running. Try again after it finishes.")
            return

        proposal.status = "running"
        proposal.approved_by = user_id
        await channel.send(f"Approved proposal `{proposal_id}`. I am running `{ACTION_LABELS[proposal.action]}` now.")
        try:
            async with lock:
                result = await self._execute_action(proposal)
            proposal.status = "completed"
            proposal.result = result
            self._pending.pop(proposal_id, None)
            await channel.send(embed=self._result_embed(proposal, result, success=True))
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event(
                    "info",
                    "ai_operator",
                    "Approved action completed.",
                    {"proposal_id": proposal_id, "action": proposal.action},
                )
        except Exception as exc:
            log.exception("Approved AI operator action failed")
            proposal.status = "failed"
            result = sanitize_text(repr(exc))[:1500]
            await channel.send(embed=self._result_embed(proposal, result, success=False))
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event(
                    "error",
                    "ai_operator",
                    "Approved action failed.",
                    {"proposal_id": proposal_id, "action": proposal.action, "error": result},
                )

    def _schedule_path(self) -> Path:
        return Path(bot_config.AI_OPERATOR_SCHEDULES_PATH)

    def _load_schedules(self) -> list[dict[str, Any]]:
        path = self._schedule_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except Exception:
            log.warning("Could not read AI operator schedules", exc_info=True)
        return []

    def _save_schedules(self, items: list[dict[str, Any]]) -> None:
        path = self._schedule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _parse_clock(text: str) -> tuple[int, int]:
        clean = sanitize_text(text).lower()
        match = re.search(r"(?:jam|at|pukul)\s*(\d{1,2})(?:[:.](\d{2}))?", clean)
        if not match:
            match = re.search(r"\b(\d{1,2})(?:[:.](\d{2}))\b", clean)
        if not match:
            return 2, 0
        hour = max(0, min(int(match.group(1)), 23))
        minute = max(0, min(int(match.group(2) or "0"), 59))
        return hour, minute

    @staticmethod
    def _weekday_from_text(text: str) -> int | None:
        lower = sanitize_text(text).lower()
        days = {
            "senin": 0,
            "monday": 0,
            "mon": 0,
            "selasa": 1,
            "tuesday": 1,
            "tue": 1,
            "rabu": 2,
            "wednesday": 2,
            "wed": 2,
            "kamis": 3,
            "thursday": 3,
            "thu": 3,
            "jumat": 4,
            "jum'at": 4,
            "friday": 4,
            "fri": 4,
            "sabtu": 5,
            "saturday": 5,
            "sat": 5,
            "minggu": 6,
            "ahad": 6,
            "sunday": 6,
            "sun": 6,
        }
        for word, value in days.items():
            if re.search(rf"\b{re.escape(word)}\b", lower):
                return value
        return None

    def _parse_schedule_params(self, text: str, action: str = "run_r2_maintenance") -> dict[str, Any]:
        clean = sanitize_text(text).strip()
        lower = clean.lower()
        action = action or "run_r2_maintenance"
        if "steam" in lower and ("sync" in lower or "db" in lower or "database" in lower):
            action = "run_steam_db_sync"
        elif "audit" in lower and ("server" in lower or "discord" in lower):
            action = "run_server_audit"
        elif "booster" in lower and ("sync" in lower or "sinkron" in lower):
            action = "sync_booster_roles"
        elif "ai" in lower and ("check" in lower or "cek" in lower):
            action = "run_ai_check"

        match = re.search(r"(?:every|tiap|setiap)\s+(\d+)\s*(minute|minutes|menit|hour|hours|jam)", lower)
        if match:
            amount = int(match.group(1))
            unit = match.group(2)
            interval = amount * (60 if unit in {"minute", "minutes", "menit"} else 3600)
            return {"action": action, "kind": "interval", "interval_seconds": max(60, interval), "schedule_text": clean}

        hour, minute = self._parse_clock(clean)
        weekday = self._weekday_from_text(clean)
        if weekday is not None or any(word in lower for word in ("weekly", "mingguan", "setiap minggu", "tiap minggu")):
            return {"action": action, "kind": "weekly", "weekday": weekday if weekday is not None else 6, "hour": hour, "minute": minute, "schedule_text": clean}
        return {"action": action, "kind": "daily", "hour": hour, "minute": minute, "schedule_text": clean}

    @staticmethod
    def _next_daily(hour: int, minute: int, *, from_ts: float | None = None) -> float:
        from datetime import datetime, timezone, timedelta

        now = datetime.fromtimestamp(from_ts or time.time(), tz=timezone.utc)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate.timestamp() <= now.timestamp():
            candidate += timedelta(days=1)
        return candidate.timestamp()

    @staticmethod
    def _next_weekly(weekday: int, hour: int, minute: int, *, from_ts: float | None = None) -> float:
        from datetime import datetime, timezone, timedelta

        now = datetime.fromtimestamp(from_ts or time.time(), tz=timezone.utc)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (weekday - candidate.weekday()) % 7
        if days_ahead:
            candidate += timedelta(days=days_ahead)
        if candidate.timestamp() <= now.timestamp():
            candidate += timedelta(days=7)
        return candidate.timestamp()

    def _next_run_at(self, item: dict[str, Any], *, from_ts: float | None = None) -> float:
        kind = str(item.get("kind") or "daily")
        if kind == "interval":
            return (from_ts or time.time()) + max(60, int(item.get("interval_seconds") or 3600))
        if kind == "weekly":
            return self._next_weekly(int(item.get("weekday", 6)), int(item.get("hour", 2)), int(item.get("minute", 0)), from_ts=from_ts)
        return self._next_daily(int(item.get("hour", 2)), int(item.get("minute", 0)), from_ts=from_ts)

    async def _install_schedule(self, params: dict[str, Any], *, requested_by: int | None = None) -> str:
        schedule_text = sanitize_text(str(params.get("schedule_text") or params.get("schedule") or "")).strip()
        action = sanitize_text(str(params.get("scheduled_action") or params.get("action") or "run_r2_maintenance")).strip().lower()
        parsed = self._parse_schedule_params(schedule_text or f"daily 02:00 {action}", action=action)
        scheduled_action = str(parsed.get("action") or "").strip().lower()
        allowed_scheduled = {"run_r2_maintenance", "run_steam_db_sync", "run_server_audit", "sync_booster_roles", "run_ai_check"}
        if scheduled_action not in allowed_scheduled:
            raise ValueError("This action cannot be scheduled automatically.")
        if not self._action_enabled(scheduled_action):
            raise ValueError(f"Scheduled action `{scheduled_action}` is disabled by config.")

        items = self._load_schedules()
        schedule_id = secrets.token_hex(3)
        item = {
            "id": schedule_id,
            "action": scheduled_action,
            "kind": parsed.get("kind"),
            "hour": parsed.get("hour"),
            "minute": parsed.get("minute"),
            "weekday": parsed.get("weekday"),
            "interval_seconds": parsed.get("interval_seconds"),
            "schedule_text": parsed.get("schedule_text") or schedule_text,
            "params": sanitize_data(params.get("params") if isinstance(params.get("params"), dict) else {}),
            "created_by": requested_by,
            "created_at": time.time(),
            "next_run_at": self._next_run_at(parsed),
            "enabled": True,
        }
        items.append(item)
        self._save_schedules(items)
        return (
            f"Schedule `{schedule_id}` installed for `{scheduled_action}`. "
            f"Next run creates an approval proposal at UTC timestamp {int(item['next_run_at'])}."
        )

    async def _schedule_loop(self) -> None:
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            await asyncio.sleep(max(30, int(bot_config.AI_OPERATOR_SCHEDULE_CHECK_SECONDS or 60)))
            if not bot_config.AI_OPERATOR_ENABLED or not bot_config.AI_OPERATOR_SCHEDULER_ENABLED:
                continue
            try:
                items = self._load_schedules()
                now = time.time()
                changed = False
                for item in items:
                    if not item.get("enabled", True):
                        continue
                    next_run = float(item.get("next_run_at") or 0)
                    if next_run > now:
                        continue
                    action = str(item.get("action") or "").strip().lower()
                    if action not in ACTION_LABELS or not self._action_enabled(action):
                        item["enabled"] = False
                        changed = True
                        continue
                    proposal = await self.create_proposal(
                        action=action,
                        reason=f"Scheduled operator task `{item.get('id')}` is due: {item.get('schedule_text')}",
                        impact="This scheduled task creates a normal approval proposal. It does not bypass approval.",
                        params=item.get("params") if isinstance(item.get("params"), dict) else {},
                        source=f"schedule:{item.get('id')}",
                        requested_by=int(item.get("created_by") or 0) or None,
                        dedupe=False,
                    )
                    item["last_run_at"] = now
                    item["last_proposal_id"] = proposal.proposal_id if proposal else ""
                    item["next_run_at"] = self._next_run_at(item, from_ts=now + 1)
                    changed = True
                if changed:
                    self._save_schedules(items)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("AI operator schedule loop failed", exc_info=True)

    def _result_embed(self, proposal: OperatorProposal, result: str, *, success: bool) -> discord.Embed:
        embed = discord.Embed(
            title="Approved action completed" if success else "Approved action failed",
            description=ACTION_LABELS[proposal.action],
            color=COLOR_SUCCESS if success else COLOR_ERROR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Proposal ID", value=f"`{proposal.proposal_id}`", inline=True)
        embed.add_field(name="Status", value="Completed" if success else "Failed", inline=True)
        embed.add_field(name="Result", value=sanitize_text(result)[:1024] or "-", inline=False)
        return embed

    async def _execute_action(self, proposal: OperatorProposal) -> str:
        if proposal.action == "run_ai_check":
            caretaker = getattr(self.bot, "ai_caretaker", None)
            if not caretaker:
                raise RuntimeError("AI caretaker cog is not loaded")
            result = await caretaker.trigger(
                "owner-approved-ai-check",
                context={"proposal_id": proposal.proposal_id},
                force=True,
            )
            if not result:
                return "AI caretaker did not return a report. The provider may be unavailable or already busy."
            return f"{result.status}: {result.title} - {result.summary}"

        if proposal.action == "run_server_audit":
            cog = self.bot.get_cog("ServerAdmin")
            if not cog:
                raise RuntimeError("Server admin cog is not loaded")
            summary = await cog.run_audit(automatic=False)
            return self._summary_text(summary)

        if proposal.action == "sync_booster_roles":
            cog = self.bot.get_cog("BoosterRoles")
            if not cog:
                raise RuntimeError("Booster role cog is not loaded")
            results = await cog.sync_all_guilds()
            if not results:
                return "No guilds were available for Booster role sync."
            lines = []
            total_added = total_removed = total_errors = total_checked = 0
            for item in results:
                checked = int(item.get("checked", 0) or 0)
                added = int(item.get("added", 0) or 0)
                removed = int(item.get("removed", 0) or 0)
                errors = int(item.get("errors", 0) or 0)
                total_checked += checked
                total_added += added
                total_removed += removed
                total_errors += errors
                lines.append(
                    f"{item.get('guild')}: checked={checked}, added={added}, removed={removed}, errors={errors}"
                )
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event(
                    "warning" if total_errors else "info",
                    "server_admin",
                    "Approved Booster role sync finished.",
                    {
                        "checked": total_checked,
                        "added": total_added,
                        "removed": total_removed,
                        "errors": total_errors,
                    },
                )
            return "\n".join(
                [
                    f"Checked: {total_checked}",
                    f"Added: {total_added}",
                    f"Removed: {total_removed}",
                    f"Errors: {total_errors}",
                    *lines[:5],
                ]
            )

        if proposal.action == "schedule_action":
            return await self._install_schedule(proposal.params, requested_by=proposal.requested_by or proposal.approved_by)

        if proposal.action in SERVER_CONTENT_ACTIONS:
            cog = self.bot.get_cog("ServerAdmin")
            if not cog:
                raise RuntimeError("Server admin cog is not loaded")
            handlers = {
                "send_announcement": cog.send_announcement,
                "update_rules": cog.update_rules,
                "pin_message": cog.pin_message,
                "set_channel_topic": cog.set_channel_topic,
                "create_channel": cog.create_channel,
                "configure_channel_access": cog.configure_channel_access,
                "setup_channel_template": cog.setup_channel_template,
                "create_role": cog.create_role,
                "update_role": cog.update_role,
                "delete_role": cog.delete_role,
                "timeout_member": cog.timeout_member,
                "kick_member": cog.kick_member,
                "ban_member": cog.ban_member,
                "create_webhook": cog.create_webhook,
                "delete_webhook": cog.delete_webhook,
                "update_server_settings": cog.update_server_settings,
            }
            handler = handlers.get(proposal.action)
            if not handler:
                raise RuntimeError(f"Unsupported server action: {proposal.action}")
            return await handler(proposal.params)

        if proposal.action == "run_r2_maintenance":
            cog = self.bot.get_cog("R2MaintenanceCommands")
            if not cog:
                raise RuntimeError("R2 maintenance cog is not loaded")
            lock = getattr(cog, "_lock", None)
            if lock and lock.locked():
                return "R2 maintenance is already running. No new run was started."
            summary = await cog._run_threaded(
                apply_changes=bot_config.R2_MAINTENANCE_APPLY,
                prefix=bot_config.R2_MAINTENANCE_PREFIX,
                limit=max(1, min(int(bot_config.R2_MAINTENANCE_MAX_OBJECTS or 100), 500)),
                rename_objects=bot_config.R2_MAINTENANCE_RENAME_OBJECTS,
                clean_lua=bot_config.R2_MAINTENANCE_CLEAN_LUA_COMMENTS,
                use_steam=bot_config.R2_MAINTENANCE_STEAM_LOOKUPS,
                max_steam_lookups=max(0, min(int(bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS or 0), 500)),
                use_queue=bot_config.R2_MAINTENANCE_QUEUE_ENABLED,
                fallback_to_appid=bot_config.R2_MAINTENANCE_FALLBACK_TO_APPID,
                ignore_blacklist=False,
            )
            await cog._alert_if_needed(summary, automatic=False)
            return self._summary_text(summary)

        if proposal.action == "run_steam_db_sync":
            cog = self.bot.get_cog("SteamDbSyncCommands")
            if not cog:
                raise RuntimeError("Steam DB sync cog is not loaded")
            lock = getattr(cog, "_lock", None)
            if lock and lock.locked():
                return "Steam DB sync is already running. No new run was started."
            summary = await cog._run_threaded(
                apply_changes=bot_config.STEAM_DB_SYNC_APPLY,
                include_new=bot_config.STEAM_DB_SYNC_INCLUDE_NEW,
                max_new=max(0, min(int(bot_config.STEAM_DB_SYNC_MAX_NEW or 0), 100000)),
                max_updates=max(0, min(int(bot_config.STEAM_DB_SYNC_MAX_UPDATES or 0), 100000)),
            )
            await cog._alert_if_needed(summary, automatic=False)
            return self._summary_text(summary)

        raise RuntimeError(f"Unsupported action: {proposal.action}")

    def _summary_text(self, summary: Any) -> str:
        to_fields = getattr(summary, "to_fields", None)
        fields = to_fields() if callable(to_fields) else {}
        lines = [f"{key}: {value}" for key, value in fields.items()]
        samples = list(getattr(summary, "applied_samples", None) or getattr(summary, "samples", None) or [])
        errors = list(getattr(summary, "errors", []) or [])
        if samples:
            lines.append("Samples:")
            lines.extend(f"- {sanitize_text(item)[:140]}" for item in samples[:5])
        if errors:
            lines.append("Errors:")
            lines.extend(f"- {sanitize_text(item)[:140]}" for item in errors[:5])
        return "\n".join(lines)[:1500] or "Action finished."

    async def _create_owner_requested_proposal(self, message: discord.Message, action: str) -> None:
        default_impacts = {
            "run_r2_maintenance": (
                "This can rename R2 ZIP objects and clean configured file comments if "
                "R2_MAINTENANCE_APPLY is true. It uses the current .env file."
            ),
            "run_steam_db_sync": (
                "This can update the SQLite games table from Steam if STEAM_DB_SYNC_APPLY is true. "
                "It uses the current .env file."
            ),
            "run_ai_check": "This only asks the caretaker to re-check the current sanitized bot status.",
            "run_server_audit": "This is read-only and checks Discord server permissions, roles, channels, and Booster role consistency.",
            "sync_booster_roles": (
                "This can add the Booster role to current boosters and remove it from members who no longer boost. "
                "It uses Discord premium_since status and current role hierarchy."
            ),
            "send_announcement": "This sends a plain announcement message to the selected announcement channel.",
            "update_rules": "This posts or updates the rules message in the configured rules channel.",
            "pin_message": "This pins the selected message, or the latest message in the selected channel if no message ID is provided.",
            "set_channel_topic": "This updates the selected text channel topic.",
            "create_channel": "This creates a new text channel in the server.",
            "configure_channel_access": (
                "This updates permission overwrites on an existing text channel so everyone can read "
                "but only Admin/Moderator roles can send messages."
            ),
        }
        proposal = await self.create_proposal(
            action=action,
            reason=f"Authorized user requested this action from DM: {sanitize_text(message.content)[:220]}",
            impact=default_impacts.get(action, "This action uses the current .env file."),
            params={},
            source="authorized-dm",
            requested_by=message.author.id,
            dedupe=False,
        )
        if not proposal:
            await message.channel.send("I could not create that proposal. Check whether the AI operator action is enabled.")

    @staticmethod
    def _is_vague_attachment_request(action: str, text: str) -> bool:
        content = sanitize_text(text).strip().lower()
        if not content:
            return True
        markers = {
            "seperti ini",
            "seperti tadi",
            "kayak ini",
            "kayak tadi",
            "seperti gambar",
            "gambar tadi",
            "di file",
            "in file",
            "from file",
            "like this",
            "from this",
            "this image",
            "that image",
            "previous image",
            "attached",
            "attachment",
            "dokumen",
            "document",
            "copy text",
            "copy the text",
            "salin teks",
            "ambil teks",
        }
        if any(marker in content for marker in markers):
            return True
        if action == "update_rules" and len(content) < 160:
            return True
        return False

    async def _apply_attachment_content(
        self,
        message: discord.Message,
        action: str,
        params: dict[str, Any],
    ) -> list[str]:
        attachments = list(getattr(message, "attachments", []) or [])
        warnings: list[str] = []
        if not attachments:
            cached = get_recent_attachment_text(self.bot, message.author.id)
            if action in {"send_announcement", "update_rules"} and cached:
                existing = str(params.get("content") or "").strip()
                if self._is_vague_attachment_request(action, existing):
                    params["content"] = sanitize_text(str(cached.get("text") or "")).strip()
                    params["_attachment_cache_used"] = True
                    warnings.append("Using text from the most recent readable attachment.")
            return warnings

        if action == "send_announcement":
            for attachment in attachments:
                content_type = str(getattr(attachment, "content_type", "") or "").lower()
                filename = str(getattr(attachment, "filename", "") or "").lower()
                first_url = str(getattr(attachment, "url", "") or "").strip()
                if (
                    first_url.startswith(("http://", "https://"))
                    and (content_type.startswith("image/") or filename.endswith((".png", ".jpg", ".jpeg", ".webp")))
                ):
                    params.setdefault("image_url", first_url)
                    break

        if action not in {"send_announcement", "update_rules"}:
            return warnings

        existing = str(params.get("content") or "").strip()
        should_read = action == "update_rules" or not existing
        if action == "send_announcement":
            # Read text documents for announcements. If the attachment is only a
            # decorative image and content already exists, keep it as image_url.
            should_read = not existing or any(
                not str(getattr(item, "content_type", "") or "").lower().startswith("image/")
                for item in attachments
            )
        if not should_read:
            return warnings

        result = await read_message_attachments(
            self.bot.session,
            attachments,
            purpose=f"{ACTION_LABELS.get(action, action)} content",
        )
        warnings.extend(result.warnings)
        if result.text:
            store_attachment_text(self.bot, message.author.id, result, source="ai-operator-dm")
        extracted = sanitize_text(result.text).strip()
        if not extracted:
            return warnings

        if action == "update_rules" and self._is_vague_attachment_request(action, existing):
            params["content"] = extracted
        elif not existing:
            params["content"] = extracted
        else:
            params["content"] = f"{existing}\n\n{extracted}".strip()
        return warnings

    async def _create_server_content_proposal(
        self,
        message: discord.Message,
        action: str,
        params: dict[str, Any],
    ) -> None:
        attachment_warnings = await self._apply_attachment_content(message, action, params)
        attachment_cache_used = bool(params.pop("_attachment_cache_used", False))

        missing = []
        if action in {"send_announcement", "update_rules"} and not str(params.get("content") or "").strip():
            missing.append("content")
        if action == "set_channel_topic" and not str(params.get("topic") or "").strip():
            missing.append("topic")
        if action == "create_channel" and not str(params.get("name") or "").strip():
            missing.append("name")
        if action == "configure_channel_access" and not str(params.get("channel") or params.get("channel_id") or "").strip():
            missing.append("channel")
        if action == "setup_channel_template" and not str(params.get("category") or params.get("name") or "").strip():
            missing.append("category")
        if action == "create_role" and not str(params.get("name") or "").strip():
            missing.append("name")
        if action in {"update_role", "delete_role"} and not str(params.get("role") or params.get("role_id") or params.get("role_name") or "").strip():
            missing.append("role")
        if action in {"timeout_member", "kick_member", "ban_member"} and not str(params.get("member") or params.get("member_id") or params.get("user_id") or "").strip():
            missing.append("member")
        if action == "create_webhook" and not str(params.get("channel") or params.get("channel_id") or "").strip():
            missing.append("channel")
        if action == "delete_webhook" and not str(params.get("webhook_id") or params.get("id") or "").strip():
            missing.append("webhook_id")
        if action == "schedule_action" and not str(params.get("schedule_text") or params.get("schedule") or "").strip():
            missing.append("schedule_text")

        if missing:
            self._drafts[message.author.id] = {"action": action, "params": params, "missing": missing}
            examples = {
                "send_announcement": 'Reply with the announcement text, for example: `Test`',
                "update_rules": "Reply with the full rules text.",
                "set_channel_topic": "Reply with the new topic text.",
                "create_channel": "Reply with the channel name.",
                "pin_message": "Reply with the message ID or include the target channel.",
                "configure_channel_access": "Reply with the target channel, for example: `#welcome`.",
                "setup_channel_template": "Reply with category/template, for example: `Games` or `template game category Games`.",
                "create_role": "Reply with the role name, for example: `Donor`.",
                "update_role": "Reply with the target role name or ID.",
                "delete_role": "Reply with the target role name or ID.",
                "timeout_member": "Reply with a user mention/ID and duration, for example: `123456789012345678 10m`.",
                "kick_member": "Reply with a user mention or ID.",
                "ban_member": "Reply with a user mention or ID.",
                "create_webhook": "Reply with the target channel, for example: `#logs`.",
                "delete_webhook": "Reply with the webhook ID.",
                "schedule_action": "Reply with a schedule, for example: `setiap Minggu jam 2 pagi jalankan R2 maintenance`.",
            }
            warning_text = ""
            if attachment_warnings:
                warning_text = "\nAttachment note: " + "; ".join(attachment_warnings[:3])
            await message.channel.send(
                f"I need `{', '.join(missing)}` before I can create the proposal. "
                f"{examples.get(action, 'Reply with the missing value.')}"
                f"{warning_text}"
            )
            return

        impact = {
            "send_announcement": "This sends a public announcement message to the selected channel after approval.",
            "update_rules": "This posts or edits the server rules as plain messages after approval.",
            "pin_message": "This pins a message in the selected channel after approval.",
            "set_channel_topic": "This changes the selected channel topic after approval.",
            "create_channel": "This creates a text channel after approval.",
            "configure_channel_access": (
                "This changes channel permission overwrites after approval so everyone can read, "
                "but only Admin/Moderator roles can send messages."
            ),
            "setup_channel_template": "This creates or configures a category with standard text/voice channels after approval.",
            "create_role": "This creates or configures a Discord role after approval.",
            "update_role": "This edits an existing Discord role after approval.",
            "delete_role": "This deletes an existing Discord role after approval.",
            "timeout_member": "This applies a Discord timeout to a member after approval.",
            "kick_member": "This kicks a member after approval.",
            "ban_member": "This bans a member after approval.",
            "create_webhook": "This creates a webhook after approval. The webhook token is never shown.",
            "delete_webhook": "This deletes a webhook after approval.",
            "update_server_settings": "This edits supported server settings after approval.",
            "schedule_action": "This stores a scheduled task that creates future approval proposals. It does not bypass approval.",
        }.get(action, "This changes Discord server content after approval.")
        proposal = await self.create_proposal(
            action=action,
            reason=f"Authorized user requested Discord server content action from DM: {sanitize_text(message.content)[:220]}",
            impact=impact,
            params=params,
            source="authorized-dm",
            requested_by=message.author.id,
            dedupe=False,
        )
        if proposal:
            if attachment_cache_used:
                clear_recent_attachment_text(self.bot, message.author.id)
            warning_text = ""
            if attachment_warnings:
                warning_text = "\nAttachment note: " + "; ".join(attachment_warnings[:3])
            await message.channel.send(
                f"Proposal `{proposal.proposal_id}` is ready for `{ACTION_LABELS[action]}`. "
                f"Reply `approve {proposal.proposal_id}` to execute it."
                f"{warning_text}"
            )
        else:
            await message.channel.send("I could not create that proposal. Check whether the action is enabled.")

    async def _update_pending_proposal(self, message: discord.Message) -> bool:
        proposal, updates = self._parse_pending_update(message.content, message.author.id)
        if not proposal or not updates:
            return False

        proposal.params.update(sanitize_data(updates))
        proposal.reason = (
            f"Authorized user updated proposal from DM: {sanitize_text(message.content)[:220]}"
        )
        proposal.expires_at = time.time() + int(bot_config.AI_OPERATOR_APPROVAL_TTL_SECONDS or 900)

        changed = ", ".join(f"`{key}`" for key in updates)
        await message.channel.send(
            f"Updated proposal `{proposal.proposal_id}` ({changed}). "
            f"Reply `approve {proposal.proposal_id}` to execute it, or `reject {proposal.proposal_id}` to cancel."
        )
        await message.channel.send(embed=self._proposal_embed(proposal))
        return True

    async def _continue_draft(self, message: discord.Message) -> bool:
        draft = self._drafts.get(message.author.id)
        if not draft:
            return False
        text = sanitize_text(message.content).strip()
        attachments = list(getattr(message, "attachments", []) or [])
        if not text and not attachments:
            return True
        action = draft["action"]
        params = dict(draft.get("params") or {})
        if text:
            if action in {"send_announcement", "update_rules"}:
                value = self._strip_value_prefix(text)
                params["content"] = value
            elif action == "set_channel_topic":
                value = self._strip_value_prefix(text, field="topic")
                params["topic"] = value
            elif action == "create_channel":
                value = self._strip_value_prefix(text, field="name")
                params["name"] = value
            elif action == "pin_message":
                value = self._strip_value_prefix(text)
                params["message_id"] = value
            elif action == "configure_channel_access":
                params["channel"] = self._extract_channel_reference(text) or self._strip_value_prefix(text)
            elif action == "setup_channel_template":
                params["category"] = self._strip_value_prefix(text)
            elif action == "create_role":
                params["name"] = self._strip_value_prefix(text)
            elif action in {"update_role", "delete_role"}:
                params["role"] = self._strip_value_prefix(text)
            elif action in {"timeout_member", "kick_member", "ban_member"}:
                member_match = re.search(r"(<@!?\d+>|\b\d{16,25}\b|@[^\s]+)", text)
                params["member"] = member_match.group(1) if member_match else self._strip_value_prefix(text)
            elif action == "create_webhook":
                params["channel"] = self._extract_channel_reference(text) or self._strip_value_prefix(text)
            elif action == "delete_webhook":
                params["webhook_id"] = self._strip_value_prefix(text)
            elif action == "schedule_action":
                params["schedule_text"] = self._strip_value_prefix(text)
        self._drafts.pop(message.author.id, None)
        await self._create_server_content_proposal(message, action, params)
        return True

    def _bot_was_addressed(self, message: discord.Message) -> bool:
        bot_user = getattr(self.bot, "user", None)
        if not bot_user:
            return False
        if bot_user in getattr(message, "mentions", []) or message.mention_everyone:
            return True
        reference = getattr(message, "reference", None)
        resolved = getattr(reference, "resolved", None) if reference else None
        author = getattr(resolved, "author", None)
        if author and getattr(author, "id", None) == bot_user.id:
            return True
        content = sanitize_text(getattr(message, "content", "") or "").strip().lower()
        return content.startswith(("triadbot ", "triadbot,", "triadbot:"))

    def _strip_bot_addressing(self, text: str) -> str:
        clean = sanitize_text(text).strip()
        bot_user = getattr(self.bot, "user", None)
        if bot_user:
            clean = clean.replace(f"<@{bot_user.id}>", "")
            clean = clean.replace(f"<@!{bot_user.id}>", "")
        clean = re.sub(r"^triadbot\s*[:,]?\s*", "", clean, flags=re.I).strip()
        return clean

    def _with_prompt_text(self, message: discord.Message, text: str):
        class _PromptMessage:
            __slots__ = ("_message", "content")

            def __init__(self, original: discord.Message, content: str):
                self._message = original
                self.content = content

            def __getattr__(self, name: str):
                return getattr(self._message, name)

        return _PromptMessage(message, text)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not bot_config.AI_OPERATOR_ENABLED:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        if not is_dm:
            # Production safety: server/database/operator control is private-DM only.
            # Public channels may still use AI chat for server information, but they
            # must never create/approve proposals or trigger maintenance actions.
            if getattr(bot_config, "AI_OPERATOR_DM_ONLY", True):
                return
            if not getattr(bot_config, "AI_OPERATOR_SERVER_PROMPTS_ENABLED", False):
                return
            if not getattr(message, "guild", None):
                return
            configured = set(getattr(bot_config, "SERVER_ADMIN_GUILD_IDS", set()) or set())
            if configured and message.guild.id not in configured:
                return
            if getattr(bot_config, "AI_OPERATOR_SERVER_REQUIRE_MENTION", True) and not self._bot_was_addressed(message):
                return

        access_allowed, access_level, access_reason = await self._operator_access_for(message.author.id)
        if not access_allowed:
            return

        prompt_text = self._strip_bot_addressing(message.content) if not is_dm else sanitize_text(message.content).strip()
        prompt_message = self._with_prompt_text(message, prompt_text)

        command, proposal_id = self._parse_operator_command(prompt_text)
        if command == "pending":
            await self._send_pending(message.channel)
            return
        if command == "reject_all":
            await self._reject_all(message.author.id, message.channel)
            return
        if command == "approve_all":
            await self._approve_all(message.author.id, message.channel)
            return
        if command == "reject" and proposal_id:
            await self._reject(proposal_id, message.author.id, message.channel)
            return
        if command == "approve" and proposal_id:
            await self._approve(proposal_id, message.author.id, message.channel)
            return
        if command == "approve":
            await self._approve(None, message.author.id, message.channel)
            return
        if command == "reject":
            await self._reject(None, message.author.id, message.channel)
            return

        if await self._continue_draft(prompt_message):
            return

        if await self._update_pending_proposal(prompt_message):
            return

        server_action, params = self._parse_server_content_request(prompt_text)
        if server_action:
            await self._create_server_content_proposal(prompt_message, server_action, params)
            return

        action = self._parse_action_request(prompt_text)
        if action:
            await self._create_owner_requested_proposal(prompt_message, action)


async def setup(bot):
    await bot.add_cog(AIOperator(bot))
