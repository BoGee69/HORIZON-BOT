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

log = logging.getLogger(__name__)

ACTION_LABELS = {
    "run_r2_maintenance": "Run R2 maintenance",
    "run_steam_db_sync": "Run Steam DB sync",
    "run_ai_check": "Run AI caretaker check",
    "run_server_audit": "Run Discord server audit",
    "sync_booster_roles": "Sync Booster roles",
}

WRITE_ACTIONS = {"run_r2_maintenance", "run_steam_db_sync", "sync_booster_roles"}


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
        self._execution_lock = asyncio.Lock()
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

    def _parse_operator_command(self, text: str) -> tuple[str, Optional[str]]:
        clean = sanitize_text(text).strip()
        lower = clean.lower()
        match = re.fullmatch(r"(approve|approved|acc|setuju|yes)\s+([a-f0-9]{6})", lower)
        if match:
            return ("approve", match.group(2))
        match = re.fullmatch(r"(reject|rejected|deny|cancel|tolak|batal|no)\s+([a-f0-9]{6})", lower)
        if match:
            return ("reject", match.group(2))
        if lower in {"pending approvals", "pending approval", "approval pending", "daftar approval", "approval"}:
            return ("pending", None)
        return ("", None)

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
        return None

    def is_operator_command(self, text: str, user_id: int) -> bool:
        if not bot_config.AI_OPERATOR_ENABLED or not self._is_owner(user_id):
            return False
        command, _ = self._parse_operator_command(text)
        return bool(command or self._parse_action_request(text))

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

    async def _reject(self, proposal_id: str, user_id: int, channel: discord.abc.Messageable) -> None:
        proposal = self._pending.pop(proposal_id, None)
        if not proposal or proposal.expired:
            await channel.send(f"Proposal `{proposal_id}` is not pending or has expired.")
            return
        proposal.status = "rejected"
        proposal.approved_by = user_id
        await channel.send(f"Rejected proposal `{proposal_id}`. No changes were made.")

    async def _approve(self, proposal_id: str, user_id: int, channel: discord.abc.Messageable) -> None:
        proposal = self._pending.get(proposal_id)
        if not proposal or proposal.expired:
            self._pending.pop(proposal_id, None)
            await channel.send(f"Proposal `{proposal_id}` is not pending or has expired.")
            return
        if proposal.status != "pending":
            await channel.send(f"Proposal `{proposal_id}` is already `{proposal.status}`.")
            return
        if self._execution_lock.locked():
            await channel.send("Another owner-approved action is already running. Try again after it finishes.")
            return

        proposal.status = "running"
        proposal.approved_by = user_id
        await channel.send(f"Approved proposal `{proposal_id}`. I am running `{ACTION_LABELS[proposal.action]}` now.")
        try:
            async with self._execution_lock:
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

        action = self._parse_action_request(message.content)
        if action:
            await self._create_owner_requested_proposal(message, action)


async def setup(bot):
    await bot.add_cog(AIOperator(bot))
