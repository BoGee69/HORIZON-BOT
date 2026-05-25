"""
TriadBot deterministic brain helpers.

This module keeps simple, safety-critical intent handling out of the LLM so
TriadBot does not force every owner/admin message into R2/database context.
It provides direct replies for time, guardian/security/caretaker reports, and
capability explanations, while leaving open-ended conversation to the AI model.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config as bot_config
from utils.ai_caretaker import sanitize_text
from utils.diagnostics import collect_health
from utils.r2_inventory import get_r2_inventory_snapshot_async


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", sanitize_text(text).lower()).strip()


def _fmt_int(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


def _summary_fields(summary: Any) -> dict[str, str]:
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


def _pick(fields: dict[str, str], keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for key in keys:
        if key in fields:
            out.append(f"- {key}: `{sanitize_text(fields[key])[:180]}`")
    return out


def _age_label(event_ts: Any) -> str:
    try:
        seconds = max(0, int(datetime.now().timestamp()) - int(event_ts or 0))
    except Exception:
        return "waktu tidak diketahui"
    if seconds < 60:
        return f"{seconds}s lalu"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m lalu"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}j lalu"
    days = hours // 24
    return f"{days}h lalu"


def _event_level(event: dict[str, Any]) -> str:
    return sanitize_text(str(event.get("level") or "INFO")).upper()


def _event_is_warning(event: dict[str, Any]) -> bool:
    return _event_level(event) in {"WARNING", "WARN", "ERROR", "CRITICAL"}


def _format_event_line(event: dict[str, Any]) -> str:
    level = _event_level(event)
    source = sanitize_text(str(event.get("source") or "unknown"))[:80]
    message = sanitize_text(str(event.get("message") or ""))[:260]
    age = _age_label(event.get("at"))
    fields = event.get("fields") or {}
    if isinstance(fields, dict):
        reason = sanitize_text(str(fields.get("reason") or fields.get("Trigger") or "")).strip()
        summary = sanitize_text(str(fields.get("summary") or "")).strip()
        extra = []
        if reason:
            extra.append(f"trigger: {reason[:80]}")
        if summary:
            extra.append(f"summary: {summary[:160]}")
        if extra:
            message = f"{message} ({'; '.join(extra)})" if message else "; ".join(extra)
    return f"- `{level}` `{source}` `{age}` — {message or 'tanpa detail'}"


def looks_like_incident_question(text: str) -> bool:
    lower = _norm(text)
    if not lower:
        return False

    explicit_warning = any(
        phrase in lower
        for phrase in (
            "warning", "peringatan", "warn", "error", "critical", "alert",
            "masalah", "problem", "issue", "trouble", "gagal", "rusak",
        )
    ) and any(
        phrase in lower
        for phrase in (
            "apa", "kenapa", "mengapa", "why", "what", "ada", "jelasin", "explain", "maksud",
        )
    )
    if explicit_warning:
        return True

    short_followups = {
        "ada apa", "ini apa", "itu apa", "apa ini", "apa itu", "kenapa ini", "kenapa itu",
        "maksudnya apa", "maksud nya apa", "ada masalah apa", "apa masalahnya",
        "what happened", "what is wrong", "what's wrong", "any warning", "any warnings",
    }
    if lower in short_followups:
        return True

    return bool(
        any(x in lower for x in ("ada apa", "apa yang terjadi", "what happened"))
        or (
            any(x in lower for x in ("tadi", "barusan", "terakhir", "latest", "last"))
            and any(x in lower for x in ("warning", "alert", "error", "masalah", "issue"))
        )
    )


async def incident_report(bot: Any, *, access_level: str = "owner") -> str:
    if access_level not in {"owner", "admin"}:
        return "Saya tidak bisa membuka detail warning internal di chat ini. Minta owner/admin cek lewat DM."

    now, tz_name = local_now()
    lines: list[str] = [
        "**Yang terjadi sekarang:**",
        f"- Waktu cek: `{now:%H:%M:%S}` `{now:%A, %d %B %Y}` ({tz_name})",
    ]

    try:
        health = await collect_health(bot)
    except Exception as exc:
        health = {"ok": False, "error": repr(exc)[:300], "checks": {}}

    checks = health.get("checks") or {}
    bad_checks = [str(name) for name, ok in checks.items() if not ok]

    events_obj = getattr(bot, "ai_events", None)
    events = events_obj.snapshot(25) if events_obj and hasattr(events_obj, "snapshot") else []
    warning_events = [event for event in events if isinstance(event, dict) and _event_is_warning(event)]
    warning_events = warning_events[-6:]

    caretaker_result = getattr(bot, "last_ai_caretaker_result", None)
    caretaker_status = sanitize_text(str(getattr(caretaker_result, "status", "") or "")).upper()

    security = getattr(bot, "ai_security", None)
    security_snapshot: dict[str, Any] = {}
    if security and hasattr(security, "snapshot"):
        try:
            snap = security.snapshot()
            if isinstance(snap, dict):
                security_snapshot = snap
        except Exception as exc:
            security_snapshot = {"error": repr(exc)[:200]}

    found_any = False

    if caretaker_result and caretaker_status and caretaker_status != "OK":
        found_any = True
        lines.append("")
        lines.append("**Warning caretaker terakhir**")
        lines.append(f"- Status: `{caretaker_status}`")
        title = sanitize_text(str(getattr(caretaker_result, "title", "") or "")).strip()
        summary = sanitize_text(str(getattr(caretaker_result, "summary", "") or "")).strip()
        if title:
            lines.append(f"- Judul: {title[:260]}")
        if summary:
            lines.append(f"- Ringkasan: {summary[:700]}")
        causes = list(getattr(caretaker_result, "causes", []) or [])
        actions = list(getattr(caretaker_result, "actions", []) or [])
        envs = list(getattr(caretaker_result, "env_to_check", []) or [])
        if causes:
            lines.append("- Kemungkinan penyebab:")
            lines.extend(f"  - {sanitize_text(str(item))[:220]}" for item in causes[:5])
        if actions:
            lines.append("- Saran tindakan:")
            lines.extend(f"  - {sanitize_text(str(item))[:220]}" for item in actions[:5])
        if envs:
            lines.append("- Env yang perlu dicek: " + ", ".join(f"`{sanitize_text(str(item))[:80]}`" for item in envs[:6]))

    if warning_events:
        found_any = True
        lines.append("")
        lines.append("**Event warning/error terbaru**")
        for event in reversed(warning_events):
            lines.append(_format_event_line(event))

    if bad_checks or health.get("error"):
        found_any = True
        lines.append("")
        lines.append("**Health check**")
        lines.append("- Checks bermasalah: " + ("`" + "`, `".join(bad_checks[:8]) + "`" if bad_checks else "`tidak ada`") )
        if health.get("error"):
            lines.append(f"- Error health: `{sanitize_text(str(health.get('error')))[:300]}`")

    recent_security = list(security_snapshot.get("recent_alerts") or []) if security_snapshot else []
    if recent_security or security_snapshot.get("error"):
        found_any = True
        lines.append("")
        lines.append("**Security guardian**")
        if security_snapshot.get("error"):
            lines.append(f"- Error security snapshot: `{sanitize_text(str(security_snapshot.get('error')))[:200]}`")
        if recent_security:
            for item in recent_security[-5:]:
                lines.append(f"- {sanitize_text(str(item))[:260]}")

    if not found_any:
        lines.append("")
        lines.append("Saya tidak menemukan warning/error aktif di event buffer runtime saat ini.")
        lines.append("Kalau ada warning yang terlihat di chat atas, kemungkinan itu pesan DM lama/attachment dari caretaker yang belum masuk ke memory chat biasa. Patch ini sekarang membuat pertanyaan seperti `ada apa?` membaca event buffer dan hasil caretaker terakhir dulu.")
    else:
        lines.append("")
        lines.append("Jadi, jangan anggap `tidak ada masalah` kalau ada blok `WARNING` di atas. Pertanyaan pendek seperti `ada apa?` sekarang harus membaca warning/event terakhir dulu, bukan menjawab ringkasan umum.")

    return "\n".join(lines[:120])


def local_now() -> tuple[datetime, str]:
    tz_name = getattr(bot_config, "BOT_TIMEZONE", "Asia/Jakarta") or "Asia/Jakarta"
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        tz_name = "Asia/Jakarta"
        tz = ZoneInfo(tz_name)
    return datetime.now(tz), tz_name


def looks_like_time_question(text: str) -> bool:
    lower = _norm(text)
    if not lower:
        return False
    time_words = (
        "jam", "pukul", "waktu", "tanggal", "tgl", "hari", "time", "clock", "date", "today", "sekarang hari",
    )
    question_words = (
        "berapa", "apa", "kapan", "sekarang", "now", "current", "today", "hari ini", "tanggal berapa", "jam berapa",
    )
    return any(word in lower for word in time_words) and any(word in lower for word in question_words)


def time_reply() -> str:
    now, tz_name = local_now()
    return f"Sekarang `{now:%H:%M:%S}` ({tz_name}).\nTanggal: `{now:%A, %d %B %Y}`."


def looks_like_capability_question(text: str) -> bool:
    lower = _norm(text)
    return bool(
        any(x in lower for x in ("lu bisa apa", "kamu bisa apa", "apa kemampuan", "capability", "capabilities", "fitur ai", "fitur kamu"))
        or (
            any(x in lower for x in ("assistant", "asisten", "security", "caretaker", "guardian"))
            and any(x in lower for x in ("bisa", "jadi", "fitur", "mode", "kerja"))
        )
    )


def capability_reply(access_level: str = "owner") -> str:
    lines = [
        "Bisa. Mode pintar saya dibagi jadi 3 bagian:",
        "",
        "**Assistant**",
        "- Jawab pertanyaan owner/admin soal bot, server, R2, SQLite, OpenDir, Steam DB sync, dan error log.",
        "- Pertanyaan biasa seperti jam/tanggal dijawab normal, tidak dipaksa masuk konteks database.",
        "",
        "**Security**",
        "- Pantau spam chat, mention flood, link flood, abuse `/gen`, dan error berulang.",
        "- Default-nya aman: saya lapor dulu. Auto-delete/timeout hanya jalan kalau env security auto-action diaktifkan.",
        "",
        "**Caretaker**",
        "- Cek health bot, SQLite, R2 inventory, rename ZIP, OpenDir sync, Steam DB sync, GitHub backup, dan proposal pending.",
        "- Aksi berbahaya tetap lewat approval card dengan `Proposal ID`.",
        "",
        "**Self-adjust code**",
        "- Saya bisa bantu diagnosa error dan menyarankan patch. Untuk edit code live, alur aman tetap: proposal patch → owner approve → commit/deploy.",
    ]
    if access_level not in {"owner", "admin"}:
        return (
            "Saya bisa bantu member soal aturan server, info channel, dan cara pakai `/gen`. "
            "Aksi server/database hanya untuk admin/owner."
        )
    return "\n".join(lines)


def looks_like_guardian_report(text: str) -> bool:
    lower = _norm(text)
    if not lower:
        return False
    report_words = ("laporan", "report", "status", "kondisi", "cek", "check", "ringkasan", "summary", "aman", "sehat")
    guardian_words = (
        "guardian", "caretaker", "security", "keamanan", "server", "bot", "database", "sqlite", "r2", "storage",
        "opendir", "open dir", "steam", "sync", "health", "sistem", "system",
    )
    if any(w in lower for w in report_words) and any(w in lower for w in guardian_words):
        return True
    return lower in {
        "server aman?", "bot aman?", "kondisi server aman?", "kondisi bot aman?", "laporan caretaker",
        "security report", "caretaker report", "guardian report", "cek semuanya", "status semuanya",
    }


async def _safe_r2_snapshot() -> dict[str, Any]:
    timeout = max(1.0, float(getattr(bot_config, "AI_CHAT_R2_STATS_TIMEOUT_SECONDS", 8) or 8))
    try:
        return await asyncio.wait_for(
            get_r2_inventory_snapshot_async(
                prefix=bot_config.R2_MAINTENANCE_PREFIX,
                cache_seconds=getattr(bot_config, "AI_CHAT_R2_STATS_CACHE_SECONDS", 900),
                max_pages=getattr(bot_config, "AI_CHAT_R2_STATS_MAX_PAGES", 2000),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return {"error": f"R2 inventory timeout setelah {timeout:.0f}s"}
    except Exception as exc:
        return {"error": repr(exc)[:300]}


async def guardian_report(bot: Any, *, access_level: str = "owner") -> str:
    now, tz_name = local_now()
    lines: list[str] = [
        "**Guardian Report**",
        f"- Waktu lokal: `{now:%H:%M:%S}` `{now:%A, %d %B %Y}` ({tz_name})",
    ]

    try:
        health = await collect_health(bot)
    except Exception as exc:
        health = {"ok": False, "error": repr(exc)[:300]}

    checks = health.get("checks") or {}
    bad_checks = [str(name) for name, ok in checks.items() if not ok]
    db = health.get("database") or {}
    lines.append("")
    lines.append("**Assistant / Bot health**")
    lines.append(f"- Overall: `{'OK' if health.get('ok') else 'DEGRADED'}`")
    lines.append(f"- Guilds: `{health.get('guilds', 0)}`")
    if db:
        lines.append(
            f"- SQLite catalog: `{_fmt_int(db.get('total_games', db.get('total', 0)))}` game, "
            f"with files: `{_fmt_int(db.get('with_files', 0))}`"
        )
    lines.append("- Checks bermasalah: " + ("`" + "`, `".join(bad_checks[:8]) + "`" if bad_checks else "`tidak ada`"))
    if health.get("error"):
        lines.append(f"- Error: `{sanitize_text(str(health.get('error')))[:300]}`")

    caretaker_result = getattr(bot, "last_ai_caretaker_result", None)
    caretaker_status = sanitize_text(str(getattr(caretaker_result, "status", "") or "")).upper()
    if caretaker_result and caretaker_status and caretaker_status != "OK":
        lines.append("")
        lines.append("**Warning caretaker aktif/terakhir**")
        lines.append(f"- Status: `{caretaker_status}`")
        title = sanitize_text(str(getattr(caretaker_result, "title", "") or "")).strip()
        summary = sanitize_text(str(getattr(caretaker_result, "summary", "") or "")).strip()
        if title:
            lines.append(f"- Judul: {title[:220]}")
        if summary:
            lines.append(f"- Ringkasan: {summary[:500]}")

    inventory = await _safe_r2_snapshot()
    lines.append("")
    lines.append("**Caretaker / R2 storage**")
    if inventory.get("error"):
        lines.append(f"- Inventory: `ERROR` — {sanitize_text(str(inventory.get('error')))[:300]}")
    else:
        total = int(inventory.get("zip_objects_counted") or 0)
        named = int(inventory.get("named_zip_objects_counted") or 0)
        appid_only = int(inventory.get("appid_only_zip_objects_counted") or 0)
        unknown = int(inventory.get("unknown_zip_objects_counted") or 0)
        pending = appid_only + unknown
        pct = round((named / total) * 100, 1) if total else 0.0
        lines.extend([
            f"- Total ZIP: `{total:,}`",
            f"- Sudah rapi: `{named:,}` (`{pct}%`)",
            f"- AppID-only: `{appid_only:,}`",
            f"- Format belum dikenali: `{unknown:,}`",
            f"- Sisa perlu dirapikan: `{pending:,}`",
            f"- Source: `{sanitize_text(str(inventory.get('source') or 'unknown'))}`",
        ])
        if inventory.get("cache_age_seconds") is not None:
            lines.append(f"- Umur cache: `{int(inventory.get('cache_age_seconds') or 0)}s`")

    security = getattr(bot, "ai_security", None)
    lines.append("")
    lines.append("**Security**")
    if security and hasattr(security, "snapshot"):
        try:
            snap = security.snapshot()
        except Exception as exc:
            snap = {"error": repr(exc)[:200]}
        if snap.get("error"):
            lines.append(f"- Security snapshot error: `{sanitize_text(str(snap.get('error')))[:200]}`")
        else:
            lines.extend([
                f"- Mode: `{'aktif' if snap.get('enabled') else 'off'}`",
                f"- Alerts sejak start: `{_fmt_int(snap.get('alerts_total'))}`",
                f"- Spam flags: `{_fmt_int(snap.get('spam_flags'))}`",
                f"- Link flags: `{_fmt_int(snap.get('link_flags'))}`",
                f"- Mention flags: `{_fmt_int(snap.get('mention_flags'))}`",
                f"- Auto-action: `{'on' if snap.get('auto_action_enabled') else 'off'}`",
            ])
            recent = snap.get("recent_alerts") or []
            if recent:
                lines.append("- Alert terakhir: " + "; ".join(f"`{sanitize_text(str(x))[:80]}`" for x in recent[-3:]))
            else:
                lines.append("- Alert terakhir: `tidak ada`")
    else:
        lines.append("- AI Security cog belum aktif atau belum keload.")

    operator = getattr(bot, "ai_operator", None)
    lines.append("")
    lines.append("**Operator / approval**")
    if operator and hasattr(operator, "_pending_proposals"):
        try:
            pending = operator._pending_proposals()
        except Exception:
            pending = []
        lines.append(f"- Pending proposal: `{len(pending)}`")
        for proposal in pending[:5]:
            label = getattr(proposal, "action", "unknown")
            pid = getattr(proposal, "proposal_id", "------")
            lines.append(f"  - `{pid}` `{label}`")
    else:
        lines.append("- Operator cog belum aktif.")

    r2_fields = _summary_fields(getattr(bot, "last_r2_maintenance_summary", None))
    opendir_fields = _summary_fields(getattr(bot, "last_opendir_sync_summary", None))
    steam_fields = _summary_fields(getattr(bot, "last_steam_db_sync_summary", None))
    if r2_fields or opendir_fields or steam_fields:
        lines.append("")
        lines.append("**Job terakhir**")
    if r2_fields:
        lines.append("R2 maintenance:")
        lines.extend(_pick(r2_fields, ("Mode", "Processed", "Rename applied", "R2 ZIP objects counted", "Errors")))
    if opendir_fields:
        lines.append("OpenDir sync:")
        lines.extend(_pick(opendir_fields, ("Mode", "Games checked", "Uploaded", "Skipped", "Errors", "Elapsed")))
    if steam_fields:
        lines.append("Steam DB sync:")
        lines.extend(_pick(steam_fields, ("Fetched Steam apps", "Names updated", "New entries added", "Saved", "Errors")))

    lines.append("")
    lines.append("**Kesimpulan**")
    if health.get("ok") and not bad_checks:
        lines.append("- Bot terlihat stabil dari health check saat ini.")
    else:
        lines.append("- Ada bagian yang perlu dicek karena health check tidak full OK.")
    if caretaker_result and caretaker_status and caretaker_status != "OK":
        lines.append("- Ada warning caretaker terakhir; detailnya saya tampilkan di bagian `Warning caretaker aktif/terakhir`.")
    if security and hasattr(security, "snapshot"):
        try:
            alerts_total = int((security.snapshot() or {}).get("alerts_total") or 0)
            lines.append("- Security sedang memantau server; belum ada alert besar." if alerts_total == 0 else "- Security sudah melihat aktivitas mencurigakan; cek alert terakhir di atas.")
        except Exception:
            pass
    if access_level in {"owner", "admin"}:
        lines.append("- Untuk aksi perubahan, saya tetap akan buat `Approval required` + `Proposal ID` dulu.")
    return "\n".join(lines[:120])


async def direct_assistant_reply(bot: Any, text: str, *, access_level: str = "owner") -> str | None:
    if looks_like_time_question(text):
        return time_reply()
    if looks_like_incident_question(text):
        return await incident_report(bot, access_level=access_level)
    if looks_like_capability_question(text):
        return capability_reply(access_level)
    if looks_like_guardian_report(text):
        return await guardian_report(bot, access_level=access_level)
    return None
