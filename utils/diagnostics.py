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
        "roles": {
            "admin_role_ids": len(bot_config.ADMIN_ROLE_IDS),
            "donor_role_ids": len(bot_config.DONOR_ROLE_IDS),
            "booster_role_ids": len(bot_config.BOOSTER_ROLE_IDS),
            "admin_role_names": sorted(bot_config.ADMIN_ROLE_NAMES),
            "donor_role_names": sorted(bot_config.DONOR_ROLE_NAMES),
            "booster_role_names": sorted(bot_config.BOOSTER_ROLE_NAMES),
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
        },
    }


def yes_no(value: bool) -> str:
    return "OK" if value else "MISSING"
