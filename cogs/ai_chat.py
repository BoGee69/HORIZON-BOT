"""
Personal DM chat with HORIZON BOT through the configured AI provider.
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
from utils.ai_chat import AIChatMemory, chat_with_horizon
from utils.ai_access import resolve_ai_chat_access
from utils.ai_brain import direct_assistant_reply, looks_like_time_question as _brain_time_q, time_reply as _brain_time_reply
from utils.ai_memory import get_learning_memory, route_hint_from_learning
from utils.attachments import read_message_attachments, store_attachment_text
from utils.diagnostics import collect_health
from utils.r2_inventory import get_r2_inventory_snapshot_async
from utils.r2_rename_diagnostic import diagnose_r2_rename_leftovers_async, format_r2_rename_diagnostic

log = logging.getLogger(__name__)


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.memory = AIChatMemory(bot_config.AI_CHAT_MAX_HISTORY)
        self._locks: dict[int, asyncio.Lock] = {}
        self._last_reply_at: dict[int, float] = {}
        self.learning = get_learning_memory()

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

    def _learning_allowed_for(self, access_level: str) -> bool:
        if not getattr(bot_config, "AI_LEARNING_ENABLED", True):
            return False
        level = sanitize_text(access_level).lower().strip()
        if level == "owner":
            return True
        return level == "admin" and bool(getattr(bot_config, "AI_LEARNING_ALLOW_ADMIN", False))

    def _maybe_store_learning_correction(self, user_id: int, access_level: str, text: str) -> str | None:
        if not self._learning_allowed_for(access_level):
            return None
        memory = self.learning or get_learning_memory()
        if not memory:
            return None
        if not memory.correction_signal(text):
            return None

        ok, status, rule = memory.save_correction(
            correction_text=text,
            source_user_id=user_id,
            history=self.memory.snapshot(user_id),
            scope="owner" if sanitize_text(access_level).lower().strip() == "owner" else "admin",
        )
        if not ok:
            # Only answer as learning feedback when the user clearly tried to teach HORIZON BOT.
            lower = sanitize_text(text).lower()
            if any(word in lower for word in ("ingat", "remember", "catat", "simpan", "ke depannya", "kedepannya")):
                return f"Saya belum menyimpan rule itu. Alasan: {sanitize_text(status)[:300]}"
            return None

        if rule is None:
            return "Paham. Saya simpan koreksi itu sebagai learning memory."

        route_note = ""
        if rule.route_hint == "read_only":
            route_note = "\nEfek routing: pesan serupa akan dianggap pertanyaan/status read-only, bukan proposal/action."
        elif rule.route_hint == "action":
            route_note = "\nEfek routing: pesan serupa bisa diarahkan ke action/proposal kalau aman dan whitelisted."

        return (
            "Paham. Saya simpan sebagai learning memory.\n"
            f"- Topic: `{rule.topic}`\n"
            f"- Rule ID: `{rule.id}`\n"
            f"- Pelajaran: {rule.lesson[:700]}"
            f"{route_note}"
        )

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
            "horizon",
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
    def _wants_r2_rename_reason(text: str) -> bool:
        lower = sanitize_text(text).lower()
        if not lower:
            return False
        reason_words = (
            "kenapa", "mengapa", "alasan", "sebab", "why", "kok",
            "belum di rename", "belum rename", "belum rapi", "masih ada",
        )
        topic_words = ("r2", "rename", "renaming", "zip", "rapi", "appid", "nama")
        return any(word in lower for word in reason_words) and any(word in lower for word in topic_words)

    @staticmethod
    def _field_int(fields: dict[str, str], key: str) -> int:
        raw = sanitize_text(str(fields.get(key, "") or "")).replace(",", "").strip()
        try:
            return int(raw)
        except Exception:
            return 0

    @staticmethod
    def _append_r2_rename_reason(
        lines: list[str],
        *,
        inventory: dict,
        summary=None,
    ) -> None:
        """Append evidence-based explanation for R2 rename leftovers.

        Important: this must not present guesses as facts. It only states what
        can be proven from inventory and the latest maintenance summary. When
        per-file evidence is missing, HORIZON BOT says so plainly.
        """
        appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
        unknown = int(inventory.get("unknown_zip_objects_counted") or 0)
        pending = appid_only + unknown
        source = sanitize_text(str(inventory.get("source") or "unknown"))

        lines.append("")
        lines.append("**Analisis kenapa masih ada yang belum rename**")
        if pending <= 0:
            lines.append("- Berdasarkan inventory sekarang, tidak ada ZIP yang perlu direname.")
            return

        lines.append("**Bukti yang saya punya sekarang**")
        lines.append(f"- Inventory R2 menunjukkan `{pending:,}` ZIP belum rapi.")
        lines.append(f"- Rinciannya: AppID-only `{appid_only:,}`, format belum dikenali `{unknown:,}`.")
        lines.append(f"- Source inventory: `{source}`.")
        if inventory.get("truncated"):
            lines.append("- Catatan bukti: scan R2 terpotong, jadi angka bisa belum mencakup seluruh bucket.")

        fields = AIChat._summary_fields(summary) if summary is not None else {}
        samples = list(getattr(summary, "samples", None) or []) if summary is not None else []
        errors_list = list(getattr(summary, "errors", None) or []) if summary is not None else []

        skipped = AIChat._field_int(fields, "Skipped")
        steam_failed = AIChat._field_int(fields, "Steam lookup failed")
        blacklisted = AIChat._field_int(fields, "Blacklisted skips")
        errors = AIChat._field_int(fields, "Errors")
        processed = AIChat._field_int(fields, "Processed")
        rename_applied = AIChat._field_int(fields, "Rename applied")
        no_appid = AIChat._field_int(fields, "Skip no AppID")
        missing_name = AIChat._field_int(fields, "Skip missing game name")
        unsafe_name = AIChat._field_int(fields, "Skip unsafe game name")
        target_exists = AIChat._field_int(fields, "Skip target exists")
        oversized = AIChat._field_int(fields, "Skip oversized ZIP")

        has_maintenance_evidence = bool(
            fields or samples or errors_list or any(
                value > 0
                for value in (
                    skipped, steam_failed, blacklisted, errors, no_appid,
                    missing_name, unsafe_name, target_exists, oversized,
                )
            )
        )

        lines.append("")
        if has_maintenance_evidence:
            lines.append("**Bukti dari run maintenance terakhir**")
            if processed:
                lines.append(f"- File yang diproses pada run terakhir: `{processed:,}`.")
            if rename_applied:
                lines.append(f"- Rename yang benar-benar diterapkan pada run terakhir: `{rename_applied:,}`.")
            if skipped:
                lines.append(f"- File/object yang dilewati pada run terakhir: `{skipped:,}`.")

            reason_lines: list[str] = []
            if no_appid:
                reason_lines.append(f"tidak ada AppID yang bisa diparse: `{no_appid:,}`")
            if missing_name:
                reason_lines.append(f"nama game tidak ditemukan di DB/cache/API: `{missing_name:,}`")
            if unsafe_name:
                reason_lines.append(f"nama game tidak aman untuk nama file: `{unsafe_name:,}`")
            if target_exists:
                reason_lines.append(f"target nama baru sudah ada, jadi dilewati agar tidak overwrite: `{target_exists:,}`")
            if oversized:
                reason_lines.append(f"ZIP melewati batas ukuran untuk proses clean/comment: `{oversized:,}`")
            if steam_failed:
                reason_lines.append(f"Steam lookup gagal: `{steam_failed:,}`")
            if blacklisted:
                reason_lines.append(f"AppID masuk blacklist lookup sementara: `{blacklisted:,}`")
            if errors:
                reason_lines.append(f"error saat maintenance: `{errors:,}`")

            if reason_lines:
                lines.append("- Alasan yang terbukti dari log maintenance:")
                for item in reason_lines[:8]:
                    lines.append(f"  - {item}")
            else:
                lines.append("- Run terakhir punya ringkasan, tapi belum punya breakdown alasan skip yang detail. Jalankan maintenance sekali lagi dengan patch ini agar alasan skip tercatat per kategori.")

            if samples:
                lines.append("- Contoh bukti log:")
                for sample in samples[:5]:
                    lines.append(f"  - `{sanitize_text(str(sample))[:220]}`")
            if errors_list:
                lines.append("- Error terakhir:")
                for error in errors_list[:3]:
                    lines.append(f"  - `{sanitize_text(str(error))[:260]}`")
        else:
            lines.append("**Batas analisis**")
            lines.append("- Saya belum punya bukti per-file dari run maintenance terakhir, jadi saya tidak akan mengarang alasan pasti.")
            lines.append("- Yang bisa dipastikan dari data sekarang hanya: masih ada AppID-only dan/atau format belum dikenali di inventory R2.")

        lines.append("")
        lines.append("**Kesimpulan berbasis data**")
        if appid_only and unknown:
            lines.append(f"- Penyebab yang terbukti di level inventory: `{appid_only:,}` masih AppID-only dan `{unknown:,}` belum bisa diklasifikasikan formatnya.")
        elif appid_only:
            lines.append(f"- Penyebab yang terbukti di level inventory: `{appid_only:,}` masih AppID-only.")
        elif unknown:
            lines.append(f"- Penyebab yang terbukti di level inventory: `{unknown:,}` belum bisa diklasifikasikan formatnya.")

        if not has_maintenance_evidence:
            lines.append("- Untuk alasan pasti seperti missing DB name, Steam lookup gagal, target exists, atau no AppID, saya perlu bukti dari run maintenance/diagnostic terbaru.")
        else:
            lines.append("- Kalau butuh jawaban per-file, saya perlu menjalankan diagnostic/maintenance terbaru agar setiap file yang skip punya reason log.")

    @staticmethod
    def _looks_like_readonly_status(text: str) -> bool:
        lower = sanitize_text(text).lower()
        if not lower:
            return False
        if _brain_time_q(text):
            return False
        try:
            if route_hint_from_learning(text) == "read_only":
                return True
        except Exception:
            pass

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
            "kenapa", "mengapa", "alasan", "sebab", "diagnosa", "diagnose",
            "analisis", "analysis", "bukti", "evidence",
        )
        topic_markers = (
            "rename", "renaming", "nama", "r2", "zip", "file", "upload",
            "uploaded", "opendir", "open dir", "database", "db", "sqlite",
            "steam", "sync", "sinkron", "server", "bot", "maintenance",
        )
        return any(marker in lower for marker in status_markers) and any(marker in lower for marker in topic_markers)

    @staticmethod
    def _wants_detailed_answer(text: str) -> bool:
        lower = sanitize_text(text).lower()
        return any(marker in lower for marker in (
            "detail", "lengkap", "rinci", "rincian", "breakdown",
            "contoh", "sample", "sampel", "log", "bukti file",
            "full", "semua", "panjang", "verbose",
        ))

    async def _append_r2_diagnostic_if_needed(self, lines: list[str], text: str) -> bool:
        if not getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_ENABLED", True):
            return False
        if not self._wants_r2_rename_reason(text):
            return False
        if not getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_ON_WHY", True):
            return False

        timeout = max(2.0, float(getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_TIMEOUT_SECONDS", 25) or 25))
        try:
            diag = await asyncio.wait_for(
                diagnose_r2_rename_leftovers_async(
                    prefix=bot_config.R2_MAINTENANCE_PREFIX,
                    sample_limit=getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_LIMIT", 500),
                    use_inventory_cache=getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_USE_CACHE", True),
                    live_scan_fallback=getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_LIVE_FALLBACK", True),
                    live_scan_limit=getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_LIVE_SCAN_LIMIT", 1000),
                    target_exists_check_limit=getattr(bot_config, "AI_CHAT_R2_DIAGNOSTIC_TARGET_EXISTS_CHECKS", 100),
                ),
                timeout=timeout,
            )
            setattr(self.bot, "last_r2_rename_diagnostic", diag)
            lines.append("")
            lines.extend(format_r2_rename_diagnostic(
                diag,
                detailed=self._wants_detailed_answer(text),
            ))
            return True
        except asyncio.TimeoutError:
            lines.append("")
            lines.append("**Diagnostic sisa rename R2**")
            lines.append(f"- Diagnostic read-only melewati timeout `{timeout:.0f}s`, jadi saya hentikan agar chat tetap responsif.")
            lines.append("- Naikkan `AI_CHAT_R2_DIAGNOSTIC_TIMEOUT_SECONDS` atau turunkan `AI_CHAT_R2_DIAGNOSTIC_LIMIT` kalau perlu.")
            return True
        except Exception as exc:
            lines.append("")
            lines.append("**Diagnostic sisa rename R2**")
            lines.append(f"- Diagnostic gagal: `{sanitize_text(str(exc))[:350]}`")
            return True

    async def _direct_status_reply(self, text: str, *, language: str = "Indonesian") -> str:
        lower = sanitize_text(text).lower()
        wants_r2 = any(word in lower for word in ("r2", "rename", "renaming", "zip", "file", "maintenance", "storage", "nama"))
        wants_opendir = any(word in lower for word in ("opendir", "open dir", "upload", "uploaded", "download", "sync file"))
        wants_steam = any(word in lower for word in ("steam", "database", "db", "sqlite", "catalog", "katalog", "sync", "sinkron"))
        wants_github = any(word in lower for word in ("github", "backup", "db backup", "codex", "git"))
        wants_server = any(word in lower for word in ("server", "bot", "status", "health", "sehat", "pulse", "kondisi"))

        # Labels bilingüe
        if language == "English":
            lbl_r2 = "**R2 / ZIP rename**"
            lbl_opendir = "**OpenDir sync (last)**"
            lbl_steam = "**Steam DB sync (last)**"
            lbl_github = "**GitHub DB backup (last)**"
            lbl_server = "**Bot / server health**"
            lbl_no_r2 = "- No OpenDir sync summary in runtime memory yet."
            lbl_no_steam = "- No Steam DB sync summary in runtime memory yet."
            lbl_no_github = "- No GitHub backup summary in runtime memory yet."
            lbl_overall = "Overall"
            lbl_catalog = "SQLite catalog"
            lbl_with_files = "with files"
            lbl_checks = "Failed checks"
            lbl_none = "none"
        else:
            lbl_r2 = "**R2 / rename ZIP**"
            lbl_opendir = "**OpenDir sync terakhir**"
            lbl_steam = "**Steam DB sync terakhir**"
            lbl_github = "**GitHub DB backup terakhir**"
            lbl_server = "**Bot / server health**"
            lbl_no_r2 = "- Belum ada ringkasan OpenDir sync di memori runtime ini."
            lbl_no_steam = "- Belum ada ringkasan Steam DB sync di memori runtime ini."
            lbl_no_github = "- Belum ada ringkasan GitHub backup di memori runtime ini."
            lbl_overall = "Overall"
            lbl_catalog = "SQLite catalog"
            lbl_with_files = "with files"
            lbl_checks = "Checks bermasalah"
            lbl_none = "tidak ada"

        lines: list[str] = []

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
            lines.append(lbl_r2)
            if inventory.get("error"):
                lines.append(f"- Status inventory: error — `{sanitize_text(inventory.get('error'))[:300]}`")
            else:
                total_zip = int(inventory.get("zip_objects_counted") or 0)
                named_zip = int(inventory.get("named_zip_objects_counted") or 0)
                appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
                unknown = int(inventory.get("unknown_zip_objects_counted") or 0)
                pending = appid_only + unknown
                pct = round(named_zip / total_zip * 100, 1) if total_zip else 0.0
                if language == "English":
                    lines.extend([
                        f"Still `{pending:,}` ZIPs not properly named out of `{total_zip:,}` total.",
                        f"Properly named: `{named_zip:,}` (`{pct}%`). Remaining: appid-only `{appid_only:,}`, unknown format `{unknown:,}`.",
                    ])
                else:
                    lines.extend([
                        f"Intinya: masih ada `{pending:,}` ZIP yang belum rapi dari total `{total_zip:,}`.",
                        f"Yang sudah rapi `{named_zip:,}` (`{pct}%`). Sisanya: AppID-only `{appid_only:,}`, format tidak dikenali `{unknown:,}`.",
                    ])
                if inventory.get("truncated"):
                    lines.append("Catatan: scan R2 terpotong, jadi angka real bisa lebih tinggi." if language != "English" else "Note: R2 scan truncated, real count may be higher.")
                if self._wants_r2_rename_reason(text):
                    diagnostic_added = await self._append_r2_diagnostic_if_needed(lines, text)
                    if not diagnostic_added:
                        self._append_r2_rename_reason(
                            lines,
                            inventory=inventory,
                            summary=getattr(self.bot, "last_r2_maintenance_summary", None),
                        )

            r2_fields = self._summary_fields(getattr(self.bot, "last_r2_maintenance_summary", None))
            if r2_fields and self._wants_detailed_answer(text):
                lines.append("")
                lines.append("**Run R2 maintenance terakhir**" if language != "English" else "**Last R2 maintenance run**")
                lines.extend(self._pick_fields(r2_fields, (
                    "Mode", "Scanned", "Processed", "Rename applied",
                    "Total rename applied", "Errors",
                )))

        if wants_opendir:
            opendir_fields = self._summary_fields(getattr(self.bot, "last_opendir_sync_summary", None))
            lines.append("")
            lines.append(lbl_opendir)
            if opendir_fields:
                lines.extend(self._pick_fields(opendir_fields, (
                    "Mode", "Games total", "Games checked", "Cursor",
                    "Cycle completed", "Remote matches", "Already existed",
                    "Uploaded", "Skipped", "No match", "Cleaned files", "Errors", "Elapsed",
                )))
            else:
                lines.append(lbl_no_r2)

        if wants_steam:
            steam_fields = self._summary_fields(getattr(self.bot, "last_steam_db_sync_summary", None))
            lines.append("")
            lines.append(lbl_steam)
            if steam_fields:
                lines.extend(self._pick_fields(steam_fields, (
                    "Mode", "Fetched Steam apps", "Existing DB entries",
                    "Placeholders found", "Names updated", "New entries added",
                    "Saved", "Errors",
                )))
            else:
                lines.append(lbl_no_steam)

        if wants_github:
            github_fields = self._summary_fields(getattr(self.bot, "last_github_db_backup_summary", None))
            lines.append("")
            lines.append(lbl_github)
            if github_fields:
                lines.extend(self._pick_fields(github_fields, (
                    "Status", "Committed", "SHA", "Branch", "Elapsed", "Errors",
                )))
            else:
                lines.append(lbl_no_github)

        if wants_server or not lines:
            try:
                health = await collect_health(self.bot)
            except Exception as exc:
                health = {"ok": False, "error": sanitize_text(str(exc))[:300]}
            db = health.get("database") or {}
            checks = health.get("checks") or {}
            lines.append("")
            lines.append(lbl_server)
            lines.append(f"- {lbl_overall}: `{'OK' if health.get('ok') else 'DEGRADED'}`")
            if db:
                total_games = db.get("total_games", db.get("total", 0))
                with_files = db.get("with_files", 0)
                lines.append(f"- {lbl_catalog}: `{int(total_games or 0):,}` game, {lbl_with_files}: `{int(with_files or 0):,}`")
            if checks:
                bad = [name for name, ok in checks.items() if not ok]
                lines.append(f"- {lbl_checks}: " + ("`" + "`, `".join(bad[:8]) + "`" if bad else f"`{lbl_none}`"))
            if health.get("error"):
                lines.append(f"- Error: `{health['error']}`")

        # Fix #6: respect AI_CHAT_MAX_REPLY_CHARS, trim from bottom rather than hard line-cut
        max_chars = max(500, int(getattr(bot_config, "AI_CHAT_MAX_REPLY_CHARS", 1800) or 1800))
        result = "\n".join(line for line in lines if line is not None)
        if len(result) > max_chars:
            trimmed: list[str] = []
            used = 0
            for line in lines:
                chunk = (line or "") + "\n"
                if used + len(chunk) > max_chars - 30:
                    trimmed.append("…")
                    break
                trimmed.append(line)
                used += len(chunk)
            result = "\n".join(trimmed)
        return result.strip()
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
            github_codex = getattr(self.bot, "ai_github_codex", None)
            if github_codex:
                codex_check = getattr(github_codex, "is_github_codex_command_for_user", None)
                if callable(codex_check) and await codex_check(message.content, message.author.id):
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
            # to the operator, even when an admin mentions HORIZON BOT with an
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

                    learned_reply = None
                    if is_dm and access_allowed:
                        learned_reply = self._maybe_store_learning_correction(
                            message.author.id,
                            access_level,
                            user_message,
                        )

                    direct_reply = None
                    if learned_reply is None and is_dm and access_allowed:
                        direct_reply = await direct_assistant_reply(
                            self.bot,
                            user_message,
                            access_level=access_level,
                        )

                    if learned_reply is not None:
                        reply = learned_reply
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    elif direct_reply is not None:
                        reply = direct_reply
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    elif is_dm and access_allowed and _brain_time_q(user_message):
                        reply = _brain_time_reply()
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    elif is_dm and access_allowed and self._looks_like_readonly_status(user_message):
                        from utils.ai_chat import _detect_reply_language
                        lang = _detect_reply_language(user_message)
                        reply = await self._direct_status_reply(user_message, language=lang)
                        self.memory.append(message.author.id, "user", user_message)
                        self.memory.append(message.author.id, "assistant", reply)
                    else:
                        reply_timeout = max(
                            5.0,
                            float(getattr(bot_config, "AI_CHAT_RESPONSE_TIMEOUT_SECONDS", 60) or 60),
                        )
                        reply = await asyncio.wait_for(
                            chat_with_horizon(
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
                        # chat_with_horizon already handles memory append internally
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
                # Fix #8: preserve user message in history even on timeout
                # so the next message has context of what was asked
                self.memory.append(message.author.id, "user", user_message)
                timeout_msg = (
                    "AI provider took too long to respond, request cancelled. "
                    "Please try again in a moment."
                    if _brain_time_q(user_message) or "english" in sanitize_text(user_message).lower()
                    else "AI provider terlalu lama merespons, jadi saya hentikan request ini. "
                    "Coba kirim ulang sebentar lagi."
                )
                await message.channel.send(timeout_msg, allowed_mentions=discord.AllowedMentions.none())
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
