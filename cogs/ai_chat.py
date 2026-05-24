"""
Personal DM chat with TriadBot through the configured AI provider.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import commands

import config as bot_config
from utils.ai_caretaker import AICaretakerUnavailable, sanitize_text
from utils.ai_chat import AIChatMemory, chat_with_triadbot
from utils.ai_access import resolve_ai_chat_access
from utils.ai_brain import direct_assistant_reply
from utils.attachments import read_message_attachments, store_attachment_text
from utils.diagnostics import collect_health
from utils.r2_inventory import get_r2_inventory_snapshot_async

log = logging.getLogger(__name__)


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.memory = AIChatMemory(bot_config.AI_CHAT_MAX_HISTORY)
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_reply_at: dict[int, float] = {}

    async def _access_for(self, user_id: int) -> tuple[bool, str, str]:
        return await resolve_ai_chat_access(self.bot, user_id)

    def _cooling_down(self, user_id: int) -> bool:
        cooldown = max(0.0, float(bot_config.AI_CHAT_COOLDOWN_SECONDS or 0))
        if cooldown <= 0:
            return False
        last = self._last_reply_at.get(user_id, 0.0)
        now = time.time()
        if now - last < cooldown:
            return True
        self._last_reply_at[user_id] = now
        return False

    async def _reply_chunks(self, message: discord.Message, text: str) -> None:
        clean = sanitize_text(text).strip()
        if not clean:
            clean = "Saya belum bisa menyusun respons yang valid. Silakan kirim ulang pesan tersebut."
        chunks = [clean[i : i + 1900] for i in range(0, len(clean), 1900)]
        for chunk in chunks[:3]:
            await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    def _is_reply_to_bot(self, message: discord.Message) -> bool:
        reference = getattr(message, "reference", None)
        if not reference:
            return False
        resolved = getattr(reference, "resolved", None)
        author = getattr(resolved, "author", None)
        bot_user = getattr(self.bot, "user", None)
        return bool(bot_user and author and author.id == bot_user.id)

    def _server_reply_allowed(self, message: discord.Message) -> bool:
        if not bot_config.AI_CHAT_SERVER_REPLIES_ENABLED:
            return False
        guild = getattr(message, "guild", None)
        if not guild:
            return False
        configured = set(getattr(bot_config, "SERVER_ADMIN_GUILD_IDS", set()) or set())
        if configured and guild.id not in configured:
            return False
        bot_user = getattr(self.bot, "user", None)
        mentioned = bool(bot_user and bot_user in getattr(message, "mentions", []))
        replied = self._is_reply_to_bot(message)
        if bot_config.AI_CHAT_SERVER_REQUIRE_MENTION:
            return mentioned or replied
        lower = sanitize_text(message.content).lower()
        passive_triggers = (
            "triadbot",
            "aturan",
            "rules",
            "peraturan",
            "panduan",
            "guide",
            "resources",
            "resource",
            "r2",
            "database",
        )
        return mentioned or replied or any(item in lower for item in passive_triggers)

    def _strip_bot_mention(self, text: str) -> str:
        bot_user = getattr(self.bot, "user", None)
        clean = sanitize_text(text)
        if bot_user:
            clean = clean.replace(f"<@{bot_user.id}>", "")
            clean = clean.replace(f"<@!{bot_user.id}>", "")
        return clean.strip()

    @staticmethod
    def _looks_like_operator_control(text: str) -> bool:
        clean = re.sub(r"\s+", " ", sanitize_text(text).strip().lower()).strip(" `.,!;:")
        if not clean:
            return False

        strong_approve = r"approve|approved|accept|accepted|acc|prove|aprove|aproove|approv|approvee|setuju|lanjut|lanjutkan|continue|proceed|confirm|konfirmasi|jalankan|jalanin|gas"
        weak_approve = r"yes|ya|oke|ok"
        reject_words = r"reject|rejected|deny|cancel|tolak|batal|no"
        all_words = r"all(?:\s+of\s+them)?|semua|semuanya|all\s+proposals?|all\s+approval(?:s)?"
        latest_words = r"latest|last|terbaru|terakhir"
        id_prefix = r"(?:(?:proposal|proposal id|id)\s+)?"

        return bool(
            re.fullmatch(rf"(?:{all_words})", clean)
            or
            re.fullmatch(
                rf"(?:(?:{all_words})\s+)?(?:{strong_approve})(?:\s+(?:{all_words}|{latest_words}|{id_prefix}[a-f0-9]{{6}}))?",
                clean,
            )
            or re.fullmatch(rf"(?:{weak_approve})\s+{id_prefix}[a-f0-9]{{6}}", clean)
            or re.fullmatch(rf"[a-f0-9]{{6}}\s+(?:{strong_approve}|{weak_approve})", clean)
            or re.fullmatch(
                rf"(?:{reject_words})(?:\s+(?:{all_words}|{latest_words}|{id_prefix}[a-f0-9]{{6}}))?",
                clean,
            )
            or re.fullmatch(rf"[a-f0-9]{{6}}\s+(?:{reject_words})", clean)
            or re.fullmatch(
                r"(?:pending|list|show|daftar|lihat|cek)\s+(?:approval|approvals|proposal|proposals)|approval pending|pending approval|approval",
                clean,
            )
        )

    @staticmethod
    def _looks_like_time_question(text: str) -> bool:
        lower = re.sub(r"\s+", " ", sanitize_text(text).lower()).strip()
        if not lower:
            return False
        time_words = (
            "jam", "pukul", "waktu", "tanggal", "hari", "tgl",
            "time", "clock", "date", "today", "sekarang hari", "hari ini",
        )
        question_words = (
            "berapa", "apa", "kapan", "sekarang", "now", "current", "today",
            "hari ini", "tanggal berapa", "jam berapa",
        )
        return any(word in lower for word in time_words) and any(word in lower for word in question_words)

    @staticmethod
    def _local_now() -> tuple[datetime, str]:
        tz_name = getattr(bot_config, "BOT_TIMEZONE", "Asia/Jakarta") or "Asia/Jakarta"
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz_name = "Asia/Jakarta"
            tz = ZoneInfo(tz_name)
        return datetime.now(tz), tz_name

    def _time_reply(self) -> str:
        now, tz_name = self._local_now()
        return (
            f"Sekarang `{now:%H:%M:%S}` ({tz_name}).\n"
            f"Tanggal: `{now:%A, %d %B %Y}`."
        )

    def _wants_zip_name_stats(self, text: str, user_id: int) -> bool:
        lower = sanitize_text(text).lower()
        if self._looks_like_time_question(text):
            return False
        has_count = any(word in lower for word in ("berapa", "total", "how many", "count", "jumlah"))
        has_name_update = any(
            word in lower
            for word in ("nama", "name", "update", "updated", "rename", "renamed", "ganti")
        )
        if "zip" in lower and has_count and has_name_update:
            return True
        if has_count and any(word in lower for word in ("sekarang", "current", "now", "saat ini")):
            recent = " ".join(item["text"] for item in self.memory.snapshot(user_id)[-4:]).lower()
            if any(marker in recent for marker in ("zip", "r2", "rename", "nama game", "appid")):
                return True
            # In owner DM, short follow-ups like "totalnya sekarang berapa?"
            # usually refer to the active R2/ZIP maintenance topic.
            return "total" in lower or "berapa" in lower
        return False

    async def _zip_name_stats_reply(self) -> str:
        timeout = max(1.0, float(getattr(bot_config, "AI_CHAT_R2_STATS_TIMEOUT_SECONDS", 8) or 8))
        try:
            inventory = await asyncio.wait_for(
                get_r2_inventory_snapshot_async(
                    prefix=bot_config.R2_MAINTENANCE_PREFIX,
                    cache_seconds=0,
                    max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            return (
                "Saya belum bisa memverifikasi jumlah ZIP langsung dari R2 sekarang.\n"
                f"Scan R2 melewati timeout `{timeout:.0f}s`, jadi saya hentikan agar chat tetap responsif."
            )
        if inventory.get("error"):
            return (
                "Saya belum bisa memverifikasi jumlah nama ZIP langsung dari R2 saat ini.\n"
                f"Error: `{sanitize_text(inventory.get('error'))[:400]}`"
            )

        total_zip = int(inventory.get("zip_objects_counted") or 0)
        named_zip = int(inventory.get("named_zip_objects_counted") or 0)
        appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
        unknown = int(inventory.get("unknown_zip_objects_counted") or 0)
        remaining = appid_only + unknown
        last_summary = getattr(self.bot, "last_r2_maintenance_summary", None)
        last_renamed = getattr(last_summary, "rename_applied", None)

        lines = [
            "Saya hitung langsung dari R2, bukan dari total katalog SQLite:",
            "",
            f"- Total ZIP di R2: `{total_zip:,}`",
            f"- ZIP yang sudah format `Nama Game (AppID).zip`: `{named_zip:,}`",
            f"- ZIP yang masih AppID-only: `{appid_only:,}`",
            f"- ZIP dengan format belum dikenali: `{unknown:,}`",
            f"- Estimasi ZIP yang masih perlu dirapikan: `{remaining:,}`",
            f"- Total rename efektif di R2 sekarang: `{named_zip:,}`",
        ]
        if last_renamed is not None:
            lines.append(f"- Rename applied pada run maintenance terakhir: `{int(last_renamed):,}`")
        if inventory.get("truncated"):
            lines.append("")
            lines.append("Catatan: scan R2 terpotong karena batas halaman, jadi angka ini belum full bucket.")
        return "\n".join(lines)


    @staticmethod
    def _summary_fields(summary) -> dict[str, str]:
        if summary is None:
            return {}
        to_fields = getattr(summary, "to_fields", None)
        if callable(to_fields):
            try:
                fields = to_fields()
                if isinstance(fields, dict):
                    return {str(k): str(v) for k, v in fields.items()}
            except Exception:
                return {}
        if isinstance(summary, dict):
            return {str(k): str(v) for k, v in summary.items()}
        return {}

    @staticmethod
    def _pick_fields(fields: dict[str, str], keys: tuple[str, ...]) -> list[str]:
        lines: list[str] = []
        for key in keys:
            if key in fields:
                lines.append(f"- {key}: `{fields[key]}`")
        return lines

    @staticmethod
    def _looks_like_readonly_status(text: str) -> bool:
        lower = sanitize_text(text).lower()
        if not lower:
            return False
        if AIChat._looks_like_time_question(text):
            return False

        # Explicit run/change intents must stay with the operator, not status chat.
        action_markers = (
            "jalankan", "jalanin", "run ", "start ", "mulai", "execute",
            "buat proposal", "minta approval", "create proposal",
            "approve", "reject", "gas ", "lanjut", "lanjutkan",
            "rapikan", "rapihin", "perapihan", "bereskan", "beresin",
        )
        if any(marker in lower for marker in action_markers):
            return False

        status_markers = (
            "progress", "progres", "status", "perkembangan", "hasil",
            "selesai", "done", "beres", "udah", "sudah", "belum",
            "berapa", "jumlah", "total", "cek", "check", "gimana",
        )
        topic_markers = (
            "rename", "renaming", "nama", "r2", "zip", "file", "upload",
            "uploaded", "opendir", "open dir", "database", "db", "sqlite",
            "steam", "sync", "sinkron", "server", "bot", "maintenance",
        )
        return any(marker in lower for marker in status_markers) and any(marker in lower for marker in topic_markers)

    async def _direct_status_reply(self, text: str) -> str:
        lower = sanitize_text(text).lower()
        wants_r2 = any(word in lower for word in ("r2", "rename", "renaming", "zip", "file", "maintenance", "storage", "nama"))
        wants_opendir = any(word in lower for word in ("opendir", "open dir", "upload", "uploaded", "download", "sync file"))
        wants_steam = any(word in lower for word in ("steam", "database", "db", "sqlite", "catalog", "katalog", "sync", "sinkron"))
        wants_server = any(word in lower for word in ("server", "bot", "status", "health", "sehat"))

        lines: list[str] = ["Saya cek dari data live yang saya pegang sekarang:"]

        if wants_r2:
            timeout = max(1.0, float(getattr(bot_config, "AI_CHAT_R2_STATS_TIMEOUT_SECONDS", 8) or 8))
            try:
                inventory = await asyncio.wait_for(
                    get_r2_inventory_snapshot_async(
                        prefix=bot_config.R2_MAINTENANCE_PREFIX,
                        cache_seconds=bot_config.AI_CHAT_R2_STATS_CACHE_SECONDS,
                        max_pages=bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                inventory = {"error": f"R2 inventory timeout setelah {timeout:.0f}s"}

            lines.append("")
            lines.append("**R2 / rename ZIP**")
            if inventory.get("error"):
                lines.append(f"- Status inventory: error — `{sanitize_text(inventory.get('error'))[:300]}`")
            else:
                total_zip = int(inventory.get("zip_objects_counted") or 0)
                named_zip = int(inventory.get("named_zip_objects_counted") or 0)
                appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
                unknown = int(inventory.get("unknown_zip_objects_counted") or 0)
                pending = appid_only + unknown
                pct = round(named_zip / total_zip * 100, 1) if total_zip else 0.0
                lines.extend([
                    f"- Total ZIP: `{total_zip:,}`",
                    f"- Sudah format `Nama Game (AppID).zip`: `{named_zip:,}` (`{pct}%`)",
                    f"- Masih AppID-only: `{appid_only:,}`",
                    f"- Format belum dikenali: `{unknown:,}`",
                    f"- Sisa yang perlu dirapikan: `{pending:,}`",
                    f"- Source: `{sanitize_text(inventory.get('source') or 'unknown')}`",
                ])
                if inventory.get("cache_age_seconds") is not None:
                    lines.append(f"- Umur data cache: `{int(inventory.get('cache_age_seconds') or 0)}s`")
                if inventory.get("truncated"):
                    lines.append("- Catatan: scan R2 terpotong, angka real bisa lebih tinggi.")

            r2_fields = self._summary_fields(getattr(self.bot, "last_r2_maintenance_summary", None))
            if r2_fields:
                lines.append("")
                lines.append("**Run R2 maintenance terakhir**")
                lines.extend(self._pick_fields(r2_fields, (
                    "Mode", "Scanned", "Processed", "Rename applied",
                    "Total rename applied", "R2 ZIP objects counted",
                    "R2 ZIP already formatted", "R2 ZIP AppID-only",
                    "R2 ZIP unknown format", "Errors",
                )))

        if wants_opendir:
            opendir_fields = self._summary_fields(getattr(self.bot, "last_opendir_sync_summary", None))
            lines.append("")
            lines.append("**OpenDir sync terakhir**")
            if opendir_fields:
                lines.extend(self._pick_fields(opendir_fields, (
                    "Mode", "Games total", "Games checked", "Cursor",
                    "Cycle completed", "Remote matches", "Already existed",
                    "Uploaded", "Skipped", "No match", "Cleaned files", "Errors", "Elapsed",
                )))
            else:
                lines.append("- Belum ada ringkasan OpenDir sync di memori runtime ini.")

        if wants_steam:
            steam_fields = self._summary_fields(getattr(self.bot, "last_steam_db_sync_summary", None))
            lines.append("")
            lines.append("**Steam DB sync terakhir**")
            if steam_fields:
                lines.extend(self._pick_fields(steam_fields, (
                    "Mode", "Fetched Steam apps", "Existing DB entries",
                    "Placeholders found", "Names updated", "New entries added",
                    "Saved", "Errors",
                )))
            else:
                lines.append("- Belum ada ringkasan Steam DB sync di memori runtime ini.")

        if wants_server or len(lines) == 1:
            try:
                health = await collect_health(self.bot)
            except Exception as exc:
                health = {"ok": False, "error": sanitize_text(str(exc))[:300]}
            db = health.get("database") or {}
            checks = health.get("checks") or {}
            lines.append("")
            lines.append("**Bot / server health**")
            lines.append(f"- Overall: `{'OK' if health.get('ok') else 'DEGRADED'}`")
            if db:
                total_games = db.get("total_games", db.get("total", 0))
                with_files = db.get("with_files", 0)
                lines.append(f"- SQLite catalog: `{int(total_games or 0):,}` game, with files: `{int(with_files or 0):,}`")
            if checks:
                bad = [name for name, ok in checks.items() if not ok]
                lines.append("- Checks bermasalah: " + ("`" + "`, `".join(bad[:8]) + "`" if bad else "`tidak ada`"))
            if health.get("error"):
                lines.append(f"- Error: `{health['error']}`")

        return "\n".join(lines[:80])
    async def _message_text_with_attachments(self, message: discord.Message) -> str:
        text = sanitize_text(message.content).strip()
        attachments = list(getattr(message, "attachments", []) or [])
        if not attachments:
            return text

        result = await read_message_attachments(
            self.bot.session,
            attachments,
            purpose="personal DM chat context",
        )
        if result.text:
            store_attachment_text(self.bot, message.author.id, result, source="ai-chat-dm")
        parts = [text] if text else []
        if result.text:
            parts.append("Attachment text:\n" + result.text)
        if result.warnings:
            parts.append("Attachment notes:\n" + "; ".join(result.warnings[:4]))
        return "\n\n".join(parts).strip()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)

        if not bot_config.AI_CHAT_ENABLED:
            if is_dm:
                await message.channel.send(
                    "AI chat belum aktif. Set Railway variable `AI_CHAT_ENABLED=true`, lalu restart bot.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            return

        access_allowed, access_level, access_reason = await self._access_for(message.author.id)
        is_owner = access_level == "owner"

        if is_dm:
            if not access_allowed:
                await message.channel.send(
                    "AI chat belum mengizinkan akun Discord ini. Tambahkan user ID kamu ke Railway variable "
                    f"`AI_CHAT_ALLOWED_IDS`. User ID yang terdeteksi: `{message.author.id}`.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            operator = getattr(self.bot, "ai_operator", None)
            if operator:
                role_aware = getattr(operator, "is_operator_command_for_user", None)
                if callable(role_aware):
                    if await role_aware(message.content, message.author.id):
                        return
                elif operator.is_operator_command(message.content, message.author.id):
                    return
                # Keep approval/control phrases out of normal owner chat when
                # the operator is loaded, but do not silently block trusted
                # admins who only have chat access.
                if is_owner and self._looks_like_operator_control(message.content):
                    return
        else:
            if not self._server_reply_allowed(message):
                return
            # Public server channels are info-only. Do not hand public messages
            # to the operator, even when an admin mentions TriadBot with an
            # operator-like request. The chat prompt will explain that real
            # server/database actions must be sent through DM.
        if not message.content.strip() and not getattr(message, "attachments", None):
            return
        if self._cooling_down(message.author.id):
            return

        lock = self._locks.setdefault(message.author.id, asyncio.Lock())
        if lock.locked():
            await message.channel.send(
                "Saya masih memproses pesan sebelumnya. Mohon tunggu sebentar.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        async with lock:
            try:
                async with message.channel.typing():
                    user_message = await self._message_text_with_attachments(message)
                    if not is_dm:
                        user_message = self._strip_bot_mention(user_message)
                    if not user_message:
                        await message.channel.send(
                            "Saya belum bisa membaca isi attachment itu.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    direct_reply = None
                    if is_dm and access_allowed:
                        direct_reply = await direct_assistant_reply(
                            self.bot,
                            user_message,
                            access_level=access_level,
                        )

                    if direct_reply is not None:
                        reply = direct_reply
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    elif is_dm and access_allowed and self._looks_like_time_question(user_message):
                        reply = self._time_reply()
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    elif is_dm and access_allowed and self._looks_like_readonly_status(user_message):
                        reply = await self._direct_status_reply(user_message)
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    elif is_dm and access_allowed and self._wants_zip_name_stats(user_message, message.author.id):
                        reply = await self._zip_name_stats_reply()
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    else:
                        reply_timeout = max(
                            5.0,
                            float(getattr(bot_config, "AI_CHAT_RESPONSE_TIMEOUT_SECONDS", 60) or 60),
                        )
                        reply = await asyncio.wait_for(
                            chat_with_triadbot(
                                self.bot,
                                user_id=message.author.id,
                                user_name=str(message.author),
                                user_message=user_message,
                                memory=self.memory,
                                is_owner=is_owner,
                                user_access_level=access_level,
                                is_dm=is_dm,
                            ),
                            timeout=reply_timeout,
                        )
                await self._reply_chunks(message, reply)
                if hasattr(self.bot, "record_ai_event"):
                    self.bot.record_ai_event(
                        "info",
                        "ai_chat",
                        "AI chat replied.",
                        {"user_id": str(message.author.id), "dm": is_dm, "access_level": access_level, "access_reason": access_reason},
                    )
            except asyncio.TimeoutError:
                log.warning("AI chat response timed out")
                await message.channel.send(
                    "AI provider terlalu lama merespons, jadi saya hentikan request ini. "
                    "Coba kirim ulang sebentar lagi.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if hasattr(self.bot, "record_ai_event"):
                    self.bot.record_ai_event(
                        "warning",
                        "ai_chat",
                        "AI chat response timed out.",
                        {"timeout_seconds": str(getattr(bot_config, "AI_CHAT_RESPONSE_TIMEOUT_SECONDS", 60))},
                    )
            except AICaretakerUnavailable as exc:
                log.warning("AI chat unavailable: %s", exc)
                await message.channel.send(
                    "AI provider utama belum siap. "
                    f"Provider: `{bot_config.AI_CHAT_PROVIDER}`, model: `{bot_config.AI_CHAT_MODEL}`. "
                    "Cek API key, quota, dan nama model di .env file.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception as exc:
                log.exception("AI chat failed")
                await message.channel.send(
                    "Saya mengalami error saat menyusun jawaban. Silakan coba lagi sebentar.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if hasattr(self.bot, "record_ai_event"):
                    self.bot.record_ai_event(
                        "error",
                        "ai_chat",
                        "AI chat failed.",
                        {"error": repr(exc)[:800]},
                    )


async def setup(bot):
    await bot.add_cog(AIChat(bot))
