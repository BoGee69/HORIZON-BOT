"""
Runtime diagnostics for health checks and admin status commands.
"""
from pathlib import Path
from typing import Any

import discord

import config as bot_config
from utils.r2_presign import _PRESIGN_ENABLED


def _writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".triadbot_healthcheck"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, "writable"
    except Exception as exc:
        return False, str(exc)


def _configured(value: Any) -> bool:
    if isinstance(value, (set, list, tuple)):
        return bool(value)
    return bool(str(value or "").strip())


def _ai_provider_key_configured(provider: str) -> bool:
    provider = str(provider or "").lower()
    if provider == "gemini":
        return bool(bot_config.GEMINI_API_KEY)
    if provider == "ollama":
        return bool(bot_config.OLLAMA_API_KEY)
    return False


async def collect_health(bot) -> dict[str, Any]:
    usage_parent = bot_config.GEN_USAGE_PATH.parent
    usage_writable, usage_message = _writable(usage_parent)
    stats = bot.db.get_stats() if getattr(bot, "db", None) else {}

    checks = {
        "discord_ready": bool(bot.is_ready()),
        "http_session_open": bool(getattr(bot, "session", None) and not bot.session.closed),
        "db_loaded": bool(getattr(bot, "db", None) is not None),
        "gen_usage_path_writable": usage_writable,
        "jwt_secret_configured": len(bot_config.JWT_SECRET) >= 32,
        "web_url_configured": _configured(bot_config.WEB_URL),
        "r2_public_or_presign_configured": bool(bot_config.R2_BASE_URL or _PRESIGN_ENABLED),
        "members_intent_enabled": bool(bot.intents.members),
        "message_content_intent_enabled": bool(bot.intents.message_content),
        "admin_alert_ids_configured": bool(bot_config.ADMIN_ALERT_IDS),
    }

    return {
        "ok": all(checks.values()),
        "version": getattr(bot, "version", None),
        "uptime_seconds": int((discord.utils.utcnow() - bot.start_time).total_seconds())
        if getattr(bot, "start_time", None)
        else None,
        "guilds": len(getattr(bot, "guilds", [])),
        "checks": checks,
        "paths": {
            "gen_usage_path": str(bot_config.GEN_USAGE_PATH),
            "gen_usage_parent": str(usage_parent),
            "gen_usage_parent_status": usage_message,
        },
        "database": {
            "total_games": stats.get("total", 0),
            "with_files": stats.get("with_files", 0),
            "db_path": str(bot_config.DB_PATH),
        },
        "steam_db_sync": {
            "enabled": bool(bot_config.STEAM_DB_SYNC_ENABLED),
            "apply": bool(bot_config.STEAM_DB_SYNC_APPLY),
            "run_on_start": bool(bot_config.STEAM_DB_SYNC_RUN_ON_START),
            "start_delay_seconds": bot_config.STEAM_DB_SYNC_START_DELAY_SECONDS,
            "interval_hours": bot_config.STEAM_DB_SYNC_INTERVAL_HOURS,
            "include_new": bool(bot_config.STEAM_DB_SYNC_INCLUDE_NEW),
            "max_new": bot_config.STEAM_DB_SYNC_MAX_NEW,
            "max_updates": bot_config.STEAM_DB_SYNC_MAX_UPDATES,
            "timeout_seconds": bot_config.STEAM_DB_SYNC_TIMEOUT_SECONDS,
            "page_size": bot_config.STEAM_DB_SYNC_PAGE_SIZE,
            "include_games": bool(bot_config.STEAM_DB_SYNC_INCLUDE_GAMES),
            "include_dlc": bool(bot_config.STEAM_DB_SYNC_INCLUDE_DLC),
            "include_software": bool(bot_config.STEAM_DB_SYNC_INCLUDE_SOFTWARE),
            "steam_api_key_configured": bool(bot_config.STEAM_API_KEY.strip()),
        },
        "roles": {
            "admin_role_ids": len(bot_config.ADMIN_ROLE_IDS),
            "donor_role_ids": len(bot_config.DONOR_ROLE_IDS),
            "booster_role_ids": len(bot_config.BOOSTER_ROLE_IDS),
            "admin_role_names": sorted(bot_config.ADMIN_ROLE_NAMES),
            "donor_role_names": sorted(bot_config.DONOR_ROLE_NAMES),
            "booster_role_names": sorted(bot_config.BOOSTER_ROLE_NAMES),
        },
        "server_admin": {
            "enabled": bool(bot_config.SERVER_ADMIN_ENABLED),
            "guild_filter_count": len(bot_config.SERVER_ADMIN_GUILD_IDS),
            "audit_on_start": bool(bot_config.SERVER_ADMIN_AUDIT_ON_START),
            "interval_hours": bot_config.SERVER_ADMIN_AUDIT_INTERVAL_HOURS,
            "alert_on_issues": bool(bot_config.SERVER_ADMIN_ALERT_ON_ISSUES),
            "required_permissions": sorted(bot_config.SERVER_ADMIN_REQUIRED_PERMISSIONS),
            "last_summary": getattr(getattr(bot, "last_server_admin_summary", None), "to_dict", lambda: None)(),
        },
        "r2": {
            "public_base_url_configured": bool(bot_config.R2_BASE_URL),
            "presign_enabled": bool(_PRESIGN_ENABLED),
            "bucket_configured": bool(bot_config.R2_BUCKET_NAME),
            "link_expire_seconds": bot_config.LINK_EXPIRE_SECONDS,
        },
        "r2_maintenance": {
            "enabled": bool(bot_config.R2_MAINTENANCE_ENABLED),
            "apply": bool(bot_config.R2_MAINTENANCE_APPLY),
            "run_on_start": bool(bot_config.R2_MAINTENANCE_RUN_ON_START),
            "start_delay_seconds": bot_config.R2_MAINTENANCE_START_DELAY_SECONDS,
            "interval_hours": bot_config.R2_MAINTENANCE_INTERVAL_HOURS,
            "prefix": bot_config.R2_MAINTENANCE_PREFIX,
            "max_objects": bot_config.R2_MAINTENANCE_MAX_OBJECTS,
            "rename_objects": bool(bot_config.R2_MAINTENANCE_RENAME_OBJECTS),
            "clean_comments": bool(bot_config.R2_MAINTENANCE_CLEAN_COMMENTS),
            "clean_extensions": sorted(bot_config.R2_MAINTENANCE_CLEAN_EXTENSIONS),
            "steam_lookups": bool(bot_config.R2_MAINTENANCE_STEAM_LOOKUPS),
            "max_steam_lookups": bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS,
            "steam_delay_seconds": bot_config.R2_MAINTENANCE_STEAM_DELAY_SECONDS,
            "queue_enabled": bool(bot_config.R2_MAINTENANCE_QUEUE_ENABLED),
            "fallback_to_appid": bool(bot_config.R2_MAINTENANCE_FALLBACK_TO_APPID),
            "blacklist_threshold": bot_config.R2_MAINTENANCE_BLACKLIST_THRESHOLD,
            "state_path": str(bot_config.R2_MAINTENANCE_STATE_PATH),
        },
        "ai_caretaker": {
            "enabled": bool(bot_config.AI_MAINTENANCE_ENABLED),
            "provider": bot_config.AI_MAINTENANCE_PROVIDER,
            "model": bot_config.AI_MAINTENANCE_MODEL,
            "interval_minutes": bot_config.AI_MAINTENANCE_INTERVAL_MINUTES,
            "alert_ids_configured": bool(bot_config.AI_MAINTENANCE_ALERT_IDS),
            "gemini_api_key_configured": bool(bot_config.GEMINI_API_KEY),
            "ollama_api_key_configured": bool(bot_config.OLLAMA_API_KEY),
            "provider_api_key_configured": _ai_provider_key_configured(bot_config.AI_MAINTENANCE_PROVIDER),
            "ollama_host": bot_config.OLLAMA_HOST,
            "ollama_timeout_seconds": bot_config.OLLAMA_TIMEOUT_SECONDS,
            "dm_on_ok": bool(bot_config.AI_MAINTENANCE_DM_ON_OK),
            "dm_on_warning": bool(bot_config.AI_MAINTENANCE_DM_ON_WARNING),
            "dm_on_critical": bool(bot_config.AI_MAINTENANCE_DM_ON_CRITICAL),
        },
        "ai_chat": {
            "enabled": bool(bot_config.AI_CHAT_ENABLED),
            "provider": bot_config.AI_CHAT_PROVIDER,
            "model": bot_config.AI_CHAT_MODEL,
            "allowed_ids_configured": bool(bot_config.AI_CHAT_ALLOWED_IDS),
            "provider_api_key_configured": _ai_provider_key_configured(bot_config.AI_CHAT_PROVIDER),
            "ollama_host": bot_config.OLLAMA_HOST,
            "ollama_timeout_seconds": bot_config.OLLAMA_TIMEOUT_SECONDS,
            "max_history": bot_config.AI_CHAT_MAX_HISTORY,
            "cooldown_seconds": bot_config.AI_CHAT_COOLDOWN_SECONDS,
            "max_reply_chars": bot_config.AI_CHAT_MAX_REPLY_CHARS,
            "r2_stats_enabled": bool(bot_config.AI_CHAT_R2_STATS_ENABLED),
            "r2_stats_cache_seconds": bot_config.AI_CHAT_R2_STATS_CACHE_SECONDS,
            "r2_stats_max_pages": bot_config.AI_CHAT_R2_STATS_MAX_PAGES,
        },
        "ai_operator": {
            "enabled": bool(bot_config.AI_OPERATOR_ENABLED),
            "allowed_ids_configured": bool(bot_config.AI_OPERATOR_ALLOWED_IDS),
            "require_confirmation": bool(bot_config.AI_OPERATOR_REQUIRE_CONFIRMATION),
            "approval_ttl_seconds": bot_config.AI_OPERATOR_APPROVAL_TTL_SECONDS,
            "proposal_cooldown_seconds": bot_config.AI_OPERATOR_PROPOSAL_COOLDOWN_SECONDS,
            "max_pending": bot_config.AI_OPERATOR_MAX_PENDING,
            "allow_r2_maintenance": bool(bot_config.AI_OPERATOR_ALLOW_R2_MAINTENANCE),
            "allow_steam_db_sync": bool(bot_config.AI_OPERATOR_ALLOW_STEAM_DB_SYNC),
            "allow_ai_recheck": bool(bot_config.AI_OPERATOR_ALLOW_AI_RECHECK),
            "allow_server_audit": bool(bot_config.AI_OPERATOR_ALLOW_SERVER_AUDIT),
            "allow_booster_sync": bool(bot_config.AI_OPERATOR_ALLOW_BOOSTER_SYNC),
        },
    }


def yes_no(value: bool) -> str:
    return "OK" if value else "MISSING"
