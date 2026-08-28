"""HORIZON BOT GitHub Mode.

Owner/Admin can ask HORIZON BOT to inspect GitHub files and generate a safe code
patch proposal. Applying the proposal creates a GitHub branch and PR, not a
live edit inside Railway.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

import discord
from discord.ext import commands

import config as bot_config
from utils.ai_access import resolve_ai_operator_access
from utils.ai_caretaker import sanitize_text
from utils.github_codex import (
    CodeProposal,
    GitHubCodexError,
    GitHubRepoClient,
    apply_code_proposal,
    build_ai_code_proposal,
    normalize_repo_path,
    rank_relevant_paths,
)

log = logging.getLogger(__name__)


class AIGitHubCodex(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.pending: dict[str, CodeProposal] = {}
        self._lock = asyncio.Lock()
        bot.ai_github_codex = self

    async def cog_unload(self):
        if getattr(self.bot, "ai_github_codex", None) is self:
            self.bot.ai_github_codex = None

    async def _access_for(self, user_id: int) -> tuple[bool, str, str]:
        return await resolve_ai_operator_access(self.bot, user_id)

    @staticmethod
    def _clean_prompt(text: str) -> str:
        clean = sanitize_text(text).strip()
        clean = re.sub(r"^(@?(?:horizon|triadbot)\s*)", "", clean, flags=re.I).strip()
        return clean

    @staticmethod
    def _is_codex_intent(text: str) -> bool:
        lower = sanitize_text(text).lower()
        triggers = (
            "github", "repo", "repository", "pull request", "pr ", "horizon github", "horizon patch", "triadbot github", "triadbot patch", "baca repo", "cek repo",
            "edit code", "ubah code", "ubah kode", "perbaiki code", "perbaiki kode",
            "patch code", "buat patch", "bikin patch", "diagnosa code", "diagnosa kode",
        )
        if any(t in lower for t in triggers):
            return True
        return bool(re.search(r"\b(approve|reject|apply|cancel|lanjut|jalankan|gas|batal|tolak)\s+(?:patch|github|repo|horizon|triadbot|codex)\s+[a-f0-9]{6}\b", lower))

    async def is_github_codex_command_for_user(self, text: str, user_id: int) -> bool:
        if not getattr(bot_config, "AI_CODEX_ENABLED", True):
            return False
        allowed, _, _ = await self._access_for(user_id)
        return bool(allowed and self._is_codex_intent(text))

    def _client(self) -> GitHubRepoClient:
        return GitHubRepoClient(self.bot.session)

    def _cleanup(self) -> None:
        expired = [pid for pid, p in self.pending.items() if p.status != "pending" or p.expired]
        for pid in expired:
            self.pending.pop(pid, None)

    def _proposal_embed(self, proposal: CodeProposal) -> discord.Embed:
        color = 0xF1C40F
        if proposal.risk == "low":
            color = 0x2ECC71
        elif proposal.risk == "high":
            color = 0xE74C3C
        embed = discord.Embed(
            title="HORIZON BOT GitHub patch approval required",
            description="Saya sudah membuat proposal patch GitHub. Saya belum mengubah file apa pun sampai proposal ini di-approve.",
            color=color,
        )
        embed.add_field(name="Proposal ID", value=f"`{proposal.proposal_id}`", inline=True)
        embed.add_field(name="Risk", value=f"`{proposal.risk or 'medium'}`", inline=True)
        embed.add_field(name="Changes", value=f"`{len(proposal.changes)}` file", inline=True)
        embed.add_field(name="Summary", value=proposal.summary[:1024] or "-", inline=False)
        if proposal.changes:
            lines = []
            for change in proposal.changes[:8]:
                lines.append(f"- `{change.path}` — {change.reason[:120]}")
            embed.add_field(name="Files", value="\n".join(lines)[:1024], inline=False)
        else:
            embed.add_field(name="Files", value="Tidak ada file yang diubah. Ini proposal diagnosa saja.", inline=False)
        ttl = int(max(0, proposal.expires_at - time.time()))
        embed.add_field(name="Approve", value=f"Reply `approve patch {proposal.proposal_id}`", inline=True)
        embed.add_field(name="Reject", value=f"Reply `reject patch {proposal.proposal_id}`", inline=True)
        embed.add_field(name="Expires", value=f"in `{ttl}` seconds", inline=True)
        return embed

    def _status_text(self) -> str:
        client = self._client()
        self._cleanup()
        return (
            "**HORIZON BOT GitHub Mode**\n"
            f"- Enabled: `{bool(getattr(bot_config, 'AI_CODEX_ENABLED', True))}`\n"
            f"- Repo: `{client.repo or 'not configured'}`\n"
            f"- Base branch: `{client.base_branch}`\n"
            f"- Create PR: `{bool(getattr(bot_config, 'AI_CODEX_CREATE_PR', True))}`\n"
            f"- Pending code proposals: `{len(self.pending)}`\n"
            "- Mode: `GitHub branch + PR`, bukan edit live Railway."
        )

    async def _send_chunks(self, channel, text: str) -> None:
        clean = sanitize_text(text).strip() or "Tidak ada output."
        for chunk in [clean[i:i+1900] for i in range(0, len(clean), 1900)][:4]:
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def _extract_path(text: str) -> str:
        clean = sanitize_text(text).strip()
        match = re.search(r"(?:file|path|baca|lihat|read|show)\s+`?([\w./\\-]+)`?", clean, re.I)
        if match:
            return normalize_repo_path(match.group(1))
        backtick = re.search(r"`([^`]+)`", clean)
        if backtick:
            return normalize_repo_path(backtick.group(1))
        parts = clean.split()
        for part in reversed(parts):
            if "/" in part or ".py" in part or ".md" in part or ".json" in part:
                return normalize_repo_path(part.strip("`.,"))
        raise GitHubCodexError("Sebutkan path file. Contoh: `baca file cogs/game_commands.py`.")

    @staticmethod
    def _extract_explicit_paths(text: str) -> list[str]:
        paths = []
        for raw in re.findall(r"`([^`]+)`", text):
            if "/" in raw or "." in raw:
                try:
                    paths.append(normalize_repo_path(raw))
                except Exception:
                    pass
        for raw in re.findall(r"\b(?:cogs|utils|docs|data|Files)/[\w./-]+\b", text):
            try:
                paths.append(normalize_repo_path(raw))
            except Exception:
                pass
        seen = set()
        result = []
        for path in paths:
            if path not in seen:
                seen.add(path)
                result.append(path)
        return result

    async def _handle_status(self, message: discord.Message) -> None:
        await message.channel.send(self._status_text(), allowed_mentions=discord.AllowedMentions.none())

    async def _handle_read_file(self, message: discord.Message, text: str) -> None:
        client = self._client()
        path = self._extract_path(text)
        item = await client.get_file(path)
        preview = item.content[:1700]
        await self._send_chunks(
            message.channel,
            f"**GitHub file:** `{item.path}`\nSize: `{item.size}` bytes\nSHA: `{item.sha}`\n\n```py\n{preview}\n```",
        )

    async def _handle_search(self, message: discord.Message, text: str) -> None:
        client = self._client()
        query = re.sub(r"\b(codex|horizon|github|repo|repository|cari|search|find|file|kode|code)\b", " ", text, flags=re.I).strip()
        if not query:
            query = text
        paths = await client.get_tree_paths()
        ranked = rank_relevant_paths(paths, query, limit=20)
        if not ranked:
            await message.channel.send("Saya belum menemukan path yang cocok di repo GitHub.", allowed_mentions=discord.AllowedMentions.none())
            return
        await self._send_chunks(message.channel, "**File yang kemungkinan relevan:**\n" + "\n".join(f"- `{p}`" for p in ranked))

    async def _handle_create_proposal(self, message: discord.Message, text: str) -> None:
        async with self._lock:
            client = self._client()
            selected = self._extract_explicit_paths(text)
            proposal = await build_ai_code_proposal(
                self.bot.session,
                client=client,
                requested_by=message.author.id,
                user_prompt=text,
                selected_paths=selected,
            )
            self.pending[proposal.proposal_id] = proposal
            await message.channel.send(embed=self._proposal_embed(proposal), allowed_mentions=discord.AllowedMentions.none())
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event(
                    "warning",
                    "github-patch",
                    "HORIZON BOT GitHub patch proposal created.",
                    {"proposal_id": proposal.proposal_id, "changes": str(len(proposal.changes)), "risk": proposal.risk},
                )

    async def _approve(self, message: discord.Message, proposal_id: str) -> None:
        self._cleanup()
        proposal = self.pending.get(proposal_id)
        if not proposal or proposal.expired:
            await message.channel.send(f"Proposal patch `{proposal_id}` tidak ada atau sudah expired.", allowed_mentions=discord.AllowedMentions.none())
            return
        if proposal.status != "pending":
            await message.channel.send(f"Proposal patch `{proposal_id}` sudah `{proposal.status}`.", allowed_mentions=discord.AllowedMentions.none())
            return
        if not bool(getattr(bot_config, "AI_CODEX_ALLOW_APPLY", True)):
            await message.channel.send("Patch apply belum diizinkan di config, jadi saya tidak boleh push branch/PR.", allowed_mentions=discord.AllowedMentions.none())
            return
        proposal.status = "running"
        await message.channel.send(f"Approved patch proposal `{proposal_id}`. Saya buat branch GitHub dan PR sekarang.", allowed_mentions=discord.AllowedMentions.none())
        try:
            result = await apply_code_proposal(self._client(), proposal)
            proposal.status = "completed"
            proposal.result = result
            self.pending.pop(proposal_id, None)
            await self._send_chunks(message.channel, f"**HORIZON BOT GitHub action completed**\n{result}")
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event("warning", "github-patch", "HORIZON BOT GitHub patch proposal applied.", {"proposal_id": proposal_id, "branch": proposal.branch, "pr_url": proposal.pr_url})
        except Exception as exc:
            proposal.status = "failed"
            proposal.result = repr(exc)[:1000]
            await message.channel.send(f"Patch apply gagal: `{sanitize_text(repr(exc))[:1000]}`", allowed_mentions=discord.AllowedMentions.none())
            if hasattr(self.bot, "record_ai_event"):
                self.bot.record_ai_event("error", "github-patch", "HORIZON BOT GitHub patch proposal failed.", {"proposal_id": proposal_id, "error": repr(exc)[:800]})

    async def _reject(self, message: discord.Message, proposal_id: str) -> None:
        proposal = self.pending.pop(proposal_id, None)
        if not proposal:
            await message.channel.send(f"Proposal patch `{proposal_id}` tidak ditemukan.", allowed_mentions=discord.AllowedMentions.none())
            return
        proposal.status = "rejected"
        await message.channel.send(f"Rejected patch proposal `{proposal_id}`. Tidak ada file GitHub yang diubah.", allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not getattr(bot_config, "AI_CODEX_ENABLED", True):
            return
        if not isinstance(message.channel, discord.DMChannel):
            return
        allowed, access_level, reason = await self._access_for(message.author.id)
        if not allowed:
            return
        text = self._clean_prompt(message.content)
        if not text or not self._is_codex_intent(text):
            return

        lower = text.lower().strip()
        approve = re.fullmatch(r"(?:approve|apply|lanjut|jalankan|gas)\s+(?:patch|github|repo|horizon|triadbot|codex)\s+([a-f0-9]{6})", lower)
        reject = re.fullmatch(r"(?:reject|cancel|batal|tolak)\s+(?:patch|github|repo|horizon|triadbot|codex)\s+([a-f0-9]{6})", lower)

        try:
            async with message.channel.typing():
                if approve:
                    await self._approve(message, approve.group(1))
                    return
                if reject:
                    await self._reject(message, reject.group(1))
                    return
                if re.search(r"\b(status|info|aktif|configured|konfigurasi)\b", lower):
                    await self._handle_status(message)
                    return
                if re.search(r"\b(baca|lihat|read|show|open)\b", lower) and re.search(r"[\w.-]+/[\w./-]+|\.py|\.md|\.json|\.env\.example", lower):
                    await self._handle_read_file(message, text)
                    return
                if re.search(r"\b(cari|search|find)\b", lower):
                    await self._handle_search(message, text)
                    return
                if re.search(r"\b(perbaiki|fix|diagnosa|diagnose|patch|ubah|edit|update|bug|error)\b", lower):
                    await self._handle_create_proposal(message, text)
                    return
                await message.channel.send(
                    "HORIZON BOT GitHub Mode siap. Contoh:\n"
                    "- `github status`\n"
                    "- `cari file game_commands`\n"
                    "- `baca file cogs/game_commands.py`\n"
                    "- `perbaiki error /gen session belum siap`\n"
                    "- `approve patch <id>` setelah card proposal muncul",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except GitHubCodexError as exc:
            await message.channel.send(f"HORIZON BOT GitHub belum bisa lanjut: `{sanitize_text(str(exc))[:1200]}`", allowed_mentions=discord.AllowedMentions.none())
        except asyncio.TimeoutError:
            await message.channel.send("HORIZON BOT GitHub timeout. Coba lagi dengan path file yang lebih spesifik.", allowed_mentions=discord.AllowedMentions.none())
        except Exception as exc:
            log.exception("HORIZON BOT GitHub failed")
            await message.channel.send(f"HORIZON BOT GitHub error: `{sanitize_text(repr(exc))[:1000]}`", allowed_mentions=discord.AllowedMentions.none())


async def setup(bot):
    await bot.add_cog(AIGitHubCodex(bot))
