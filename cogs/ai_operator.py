"""
Owner-approved AI operator actions.

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
from dataclasses import dataclass, field
from typing import Any, Optional

import discord
from discord.ext import commands

import config as bot_config
from config import COLOR_ERROR, COLOR_INFO, COLOR_SUCCESS, COLOR_WARNING
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
}

SERVER_CONTENT_ACTIONS = {
    "send_announcement",
    "update_rules",
    "pin_message",
    "set_channel_topic",
    "create_channel",
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
        bot.ai_operator = self

    async def cog_unload(self):
        if getattr(self.bot, "ai_operator", None) is self:
            self.bot.ai_operator = None

    def _is_owner(self, user_id: int) -> bool:
        return bool(bot_config.AI_OPERATOR_ALLOWED_IDS and user_id in bot_config.AI_OPERATOR_ALLOWED_IDS)

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
        lower = clean.lower()
        match = re.fullmatch(r"(approve|approved|acc|setuju|yes)\s+([a-f0-9]{6})", lower)
        if match:
            return ("approve", match.group(2))
        if lower in {"approve", "approved", "acc", "setuju", "yes", "ya", "oke", "ok"}:
            return ("approve", None)
        match = re.fullmatch(r"(reject|rejected|deny|cancel|tolak|batal|no)\s+([a-f0-9]{6})", lower)
        if match:
            return ("reject", match.group(2))
        if lower in {"reject", "rejected", "deny", "cancel", "tolak", "batal", "no"}:
            return ("reject", None)
        if lower in {"pending approvals", "pending approval", "approval pending", "daftar approval", "approval"}:
            return ("pending", None)
        return ("", None)

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

    def _parse_server_content_request(self, text: str) -> tuple[Optional[str], dict[str, Any]]:
        clean = sanitize_text(text).strip()
        lower = clean.lower()
        if not clean:
            return None, {}

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
            channel, content = self._extract_channel_and_content(clean)
            if not content and not channel:
                match = re.search(r"(?:rules?|peraturan)(?:\s+server)?\s*[:\-]\s*(.+)\Z", clean, re.I | re.S)
                content = self._strip_outer_quotes(match.group(1)) if match else ""
            return "update_rules", {"channel": channel, "content": content, "title": "Server Rules", "pin": True}

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
        if not bot_config.AI_OPERATOR_ENABLED or not self._is_owner(user_id):
            return False
        command, _ = self._parse_operator_command(text)
        if command:
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
            await self.create_proposal(
                action=action,
                reason=item.get("reason") or result.summary or f"AI caretaker triggered by {reason}",
                impact=item.get("impact") or "This action will use the current Railway configuration.",
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
            title="Owner approval required",
            description=(
                "I prepared a whitelisted action. I will not change anything until you approve it."
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

    async def _send_pending(self, channel: discord.abc.Messageable) -> None:
        self._cleanup()
        if not self._pending:
            await channel.send("No pending owner approvals right now.")
            return
        lines = []
        for proposal in sorted(self._pending.values(), key=lambda item: item.created_at):
            ttl = int(max(0, proposal.expires_at - time.time()))
            lines.append(f"`{proposal.proposal_id}` - {ACTION_LABELS[proposal.action]} - expires in {ttl}s")
        await channel.send("Pending approvals:\n" + "\n".join(lines[:10]))

    def _resolve_single_pending_id(self, proposal_id: Optional[str]) -> Optional[str]:
        self._cleanup()
        if proposal_id:
            return proposal_id
        pending = [item.proposal_id for item in self._pending.values() if item.status == "pending"]
        if len(pending) == 1:
            return pending[0]
        return None

    async def _reject(self, proposal_id: Optional[str], user_id: int, channel: discord.abc.Messageable) -> None:
        proposal_id = self._resolve_single_pending_id(proposal_id)
        if not proposal_id:
            await channel.send("Tell me which proposal to reject, for example `reject abc123`.")
            return
        proposal = self._pending.pop(proposal_id, None)
        if not proposal or proposal.expired:
            await channel.send(f"Proposal `{proposal_id}` is not pending or has expired.")
            return
        proposal.status = "rejected"
        proposal.approved_by = user_id
        await channel.send(f"Rejected proposal `{proposal_id}`. No changes were made.")

    async def _approve(self, proposal_id: Optional[str], user_id: int, channel: discord.abc.Messageable) -> None:
        proposal_id = self._resolve_single_pending_id(proposal_id)
        if not proposal_id:
            await channel.send("Tell me which proposal to approve, for example `approve abc123`.")
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
                    "Owner-approved action completed.",
                    {"proposal_id": proposal_id, "action": proposal.action},
                )
        except Exception as exc:
            log.exception("Owner-approved AI operator action failed")
            proposal.status = "failed"
            result = sanitize_text(repr(exc))[:1500]
            await channel.send(embed=self._result_embed(proposal, result, success=False))
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event(
                    "error",
                    "ai_operator",
                    "Owner-approved action failed.",
                    {"proposal_id": proposal_id, "action": proposal.action, "error": result},
                )

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
                    "Owner-approved Booster role sync finished.",
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

        if proposal.action in SERVER_CONTENT_ACTIONS:
            cog = self.bot.get_cog("ServerAdmin")
            if not cog:
                raise RuntimeError("Server admin cog is not loaded")
            if proposal.action == "send_announcement":
                return await cog.send_announcement(proposal.params)
            if proposal.action == "update_rules":
                return await cog.update_rules(proposal.params)
            if proposal.action == "pin_message":
                return await cog.pin_message(proposal.params)
            if proposal.action == "set_channel_topic":
                return await cog.set_channel_topic(proposal.params)
            if proposal.action == "create_channel":
                return await cog.create_channel(proposal.params)

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
                "R2_MAINTENANCE_APPLY is true. It uses the current Railway variables."
            ),
            "run_steam_db_sync": (
                "This can update games.json from Steam if STEAM_DB_SYNC_APPLY is true. "
                "It uses the current Railway variables."
            ),
            "run_ai_check": "This only asks the caretaker to re-check the current sanitized bot status.",
            "run_server_audit": "This is read-only and checks Discord server permissions, roles, channels, and Booster role consistency.",
            "sync_booster_roles": (
                "This can add the Booster role to current boosters and remove it from members who no longer boost. "
                "It uses Discord premium_since status and current role hierarchy."
            ),
            "send_announcement": "This sends an embed announcement to the selected announcement channel.",
            "update_rules": "This posts or updates the rules message in the configured rules channel.",
            "pin_message": "This pins the selected message, or the latest message in the selected channel if no message ID is provided.",
            "set_channel_topic": "This updates the selected text channel topic.",
            "create_channel": "This creates a new text channel in the server.",
        }
        proposal = await self.create_proposal(
            action=action,
            reason=f"Owner requested this action from DM: {sanitize_text(message.content)[:220]}",
            impact=default_impacts.get(action, "This action uses the current Railway variables."),
            params={},
            source="owner-dm",
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

        if missing:
            self._drafts[message.author.id] = {"action": action, "params": params, "missing": missing}
            examples = {
                "send_announcement": 'Reply with the announcement text, for example: `Test`',
                "update_rules": "Reply with the full rules text.",
                "set_channel_topic": "Reply with the new topic text.",
                "create_channel": "Reply with the channel name.",
                "pin_message": "Reply with the message ID or include the target channel.",
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
            "send_announcement": "This sends a public announcement embed to the selected channel after approval.",
            "update_rules": "This posts or edits the server rules as plain messages after approval.",
            "pin_message": "This pins a message in the selected channel after approval.",
            "set_channel_topic": "This changes the selected channel topic after approval.",
            "create_channel": "This creates a text channel after approval.",
        }.get(action, "This changes Discord server content after approval.")
        proposal = await self.create_proposal(
            action=action,
            reason=f"Owner requested Discord server content action from DM: {sanitize_text(message.content)[:220]}",
            impact=impact,
            params=params,
            source="owner-dm",
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
            f"Owner updated proposal from DM: {sanitize_text(message.content)[:220]}"
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
        self._drafts.pop(message.author.id, None)
        await self._create_server_content_proposal(message, action, params)
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not bot_config.AI_OPERATOR_ENABLED:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        if not self._is_owner(message.author.id):
            return

        command, proposal_id = self._parse_operator_command(message.content)
        if command == "pending":
            await self._send_pending(message.channel)
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

        if await self._continue_draft(message):
            return

        if await self._update_pending_proposal(message):
            return

        server_action, params = self._parse_server_content_request(message.content)
        if server_action:
            await self._create_server_content_proposal(message, server_action, params)
            return

        action = self._parse_action_request(message.content)
        if action:
            await self._create_owner_requested_proposal(message, action)


async def setup(bot):
    await bot.add_cog(AIOperator(bot))
