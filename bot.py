"""
Steam Game Database & Download Manager Bot
"""
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import secrets
import sys
import time
import traceback
from pathlib import Path

import discord
from discord.ext import commands
import aiohttp
from aiohttp import web
import jwt

sys.path.insert(0, str(Path(__file__).parent))

import config as bot_config
from config import (
    DISCORD_TOKEN, BOT_PREFIX, BOT_VERSION, BOT_DESCRIPTION,
    LOG_LEVEL, LOG_FILE, LOG_FORMAT, LOG_DATE_FORMAT,
    JWT_SECRET, PORT,
)
from utils.alerts import AdminNotifier
from utils.ai_caretaker import CaretakerLogHandler, SafeEventRingBuffer, sanitize_data
from utils.database import DatabaseManager
from utils.diagnostics import collect_health, _deployment_info
from utils.legal_pages import PRIVACY_HTML, TERMS_HTML
from utils.n8n import post_n8n_event, summary_to_payload
from utils.r2_keys import build_r2_key_candidates
from utils.r2_presign import generate_presigned_url

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            encoding="utf-8",
            maxBytes=5 * 1024 * 1024,  # 5MB
            backupCount=3,             # Keep 3 old logs
        ),
    ],
)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# Only these slash commands should be visible in Discord. r2_recent is admin-gated
# but intentionally published so the owner can inspect fresh R2 uploads quickly.
PUBLIC_SLASH_COMMAND_ALLOWLIST = {"gen", "r2_recent", "request"}


class SteamBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=BOT_PREFIX, intents=intents, description=BOT_DESCRIPTION)
        self.version    = BOT_VERSION
        self.start_time = discord.utils.utcnow()
        self.session: aiohttp.ClientSession | None = None
        self.db         = DatabaseManager()
        self.notifier   = AdminNotifier(self)
        self.ai_events  = SafeEventRingBuffer(maxlen=60)
        self.ai_caretaker = None
        self.ai_operator = None
        self.ai_security = None
        self.last_ai_caretaker_result = None
        self.last_r2_maintenance_summary = None
        self.last_steam_db_sync_summary = None
        self.last_server_admin_summary = None
        self.last_opendir_sync_summary = None  # set by cogs/opendir_sync.py
        self.last_github_db_backup_summary = None  # set by cogs/github_db_backup.py
        self._ai_log_handler = CaretakerLogHandler(self.ai_events)
        self._ai_log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(self._ai_log_handler)
        self._ready_notified = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        log.info("✅ HTTP session created")
        deploy_info = _deployment_info()
        log.info(
            "Runtime deployment marker: version=%s commit=%s service=%s deployment=%s",
            self.version,
            deploy_info.get("commit_short") or "-",
            deploy_info.get("railway_service") or "-",
            deploy_info.get("railway_deployment_id") or "-",
        )
        self.db.load()
        await self.load_cogs()
        self.tree.on_error = self.on_app_command_error

        # Keep Discord slash UI clean: only /gen is public.
        # Other operational features keep running automatically in their cogs
        # and can be controlled through AI/operator DM prompts.
        self._prune_slash_commands()

        # FIX: asyncio.create_task() — self.loop deprecated di discord.py 2.x
        asyncio.create_task(self.start_web_server())

        try:
            synced = await self.tree.sync()
            log.info(
                "✅ Synced %d global slash command(s): %s",
                len(synced),
                ", ".join(cmd.name for cmd in synced) or "none",
            )
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")

    async def start_web_server(self):
        app = web.Application()
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/terms", self.handle_terms)
        app.router.add_get("/privacy", self.handle_privacy)
        app.router.add_get("/download/{appid}", self.handle_download)
        app.router.add_get("/n8n/health", self.handle_n8n_health)
        app.router.add_post("/n8n/health", self.handle_n8n_health)
        app.router.add_post("/n8n/event", self.handle_n8n_event)
        app.router.add_post("/n8n/opendir-sync", self.handle_n8n_opendir_sync)
        app.router.add_post("/n8n/db-backup", self.handle_n8n_db_backup)
        app.router.add_post("/n8n/steam-db-sync", self.handle_n8n_steam_db_sync)
        app.router.add_post("/n8n/r2-maintenance", self.handle_n8n_r2_maintenance)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info(f"🌐 Web API running on port {PORT}")

    async def handle_health(self, request):
        health = await collect_health(self)
        return web.json_response(health, status=200 if health["ok"] else 503)

    def _n8n_auth_error(self, request) -> web.Response | None:
        if not bot_config.N8N_ENABLED:
            return web.json_response({"ok": False, "error": "n8n integration is disabled"}, status=404)
        if not bot_config.N8N_SHARED_SECRET:
            return web.json_response({"ok": False, "error": "N8N_SHARED_SECRET is not configured"}, status=503)

        expected = bot_config.N8N_SHARED_SECRET
        candidates: list[str] = []

        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            candidates.append(auth_header.split(" ", 1)[1].strip())

        for header_name in ("X-HORIZON-N8N-Token", "X-N8N-Token"):
            header_value = request.headers.get(header_name, "").strip()
            if header_value:
                candidates.append(header_value)

        query_token = request.query.get("token", "").strip()
        if query_token:
            candidates.append(query_token)

        if any(secrets.compare_digest(token, expected) for token in candidates):
            return None
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    async def _n8n_payload(self, request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            payload = await request.json()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _payload_bool(payload: dict, name: str, default: bool = False) -> bool:
        value = payload.get(name, default)
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _payload_int(payload: dict, name: str, default: int = 0, *, minimum: int = 0, maximum: int = 100000) -> int:
        try:
            value = int(payload.get(name, default))
        except Exception:
            value = default
        return max(minimum, min(maximum, value))

    def _n8n_maintenance_error(self) -> web.Response | None:
        if not bot_config.N8N_ALLOW_MAINTENANCE_ACTIONS:
            return web.json_response(
                {
                    "ok": False,
                    "error": "N8N_ALLOW_MAINTENANCE_ACTIONS is false",
                },
                status=403,
            )
        return None

    async def emit_n8n_event(self, event_type: str, payload: dict) -> bool:
        return await post_n8n_event(getattr(self, "session", None), event_type, payload)

    async def _run_n8n_background(self, action: str, awaitable) -> None:
        started = time.time()
        try:
            result = await awaitable
            summary = summary_to_payload(result)
            ok = self._n8n_result_ok(summary)
            event_status = "completed" if ok else "failed"
            await self.emit_n8n_event(
                f"{action}.{event_status}",
                {
                    "action": action,
                    "status": event_status,
                    "ok": ok,
                    "elapsed_seconds": round(time.time() - started, 2),
                    "summary": summary,
                },
            )
        except Exception as exc:
            log.exception("n8n background action failed: %s", action)
            context = {"action": action, "error": repr(exc)[:1000]}
            self.record_ai_event("error", "n8n", f"n8n action failed: {action}", context)
            self.queue_ai_caretaker("n8n-action-failed", context, force=True)
            await self.emit_n8n_event(
                f"{action}.failed",
                {
                    "action": action,
                    "status": "failed",
                    "elapsed_seconds": round(time.time() - started, 2),
                    "error": repr(exc)[:1000],
                },
            )

    def _n8n_accepted_response(self, action: str, task: asyncio.Task) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "action": action,
                "status": "accepted",
                "task_name": task.get_name(),
                "callback": "Configure N8N_WEBHOOK_URL to receive completion events.",
            },
            status=202,
        )

    @staticmethod
    def _n8n_result_ok(summary: dict | None) -> bool:
        if not summary:
            return True
        if "ok" in summary:
            return bool(summary.get("ok"))
        return not bool(summary.get("has_errors") or summary.get("errors"))

    def _n8n_summary_response(self, action: str, result, *, status: int = 200) -> web.Response:
        summary = summary_to_payload(result)
        ok = self._n8n_result_ok(summary)
        return web.json_response(
            {
                "ok": ok,
                "action": action,
                "status": "completed" if ok else "failed",
                "summary": summary,
            },
            status=status,
        )

    async def handle_n8n_health(self, request):
        auth_error = self._n8n_auth_error(request)
        if auth_error is not None:
            return auth_error

        health = await collect_health(self)
        return web.json_response(
            {
                "ok": bool(health.get("ok")),
                "action": "health",
                "health": health,
                "recent_events": self.ai_events.snapshot() if getattr(self, "ai_events", None) else [],
            },
            status=200 if health.get("ok") else 503,
        )

    async def handle_n8n_event(self, request):
        auth_error = self._n8n_auth_error(request)
        if auth_error is not None:
            return auth_error

        payload = await self._n8n_payload(request)
        level = str(payload.get("level") or "info").strip().lower()
        if level not in {"info", "warning", "error"}:
            level = "info"
        source = str(payload.get("source") or "n8n").strip()[:80] or "n8n"
        message = str(payload.get("message") or payload.get("text") or "n8n event").strip()
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}

        self.record_ai_event(level, source, message, fields)

        delivered = 0
        if self._payload_bool(payload, "notify_admins", False):
            delivered = await self.notify_admins(
                str(payload.get("title") or "n8n event")[:256],
                message[:4000],
                level=level,
                fields=fields,
                key=str(payload.get("key") or f"n8n-{source}-{level}")[:120],
                force=self._payload_bool(payload, "force", False),
                skip_n8n=True,
            )

        return web.json_response(
            {
                "ok": True,
                "action": "event",
                "recorded": True,
                "admin_dms_delivered": delivered,
            }
        )

    async def handle_n8n_opendir_sync(self, request):
        auth_error = self._n8n_auth_error(request)
        if auth_error is not None:
            return auth_error
        maintenance_error = self._n8n_maintenance_error()
        if maintenance_error is not None:
            return maintenance_error

        payload = await self._n8n_payload(request)
        cog = self.get_cog("OpenDirSync")
        if not cog:
            return web.json_response({"ok": False, "error": "OpenDirSync cog is not loaded"}, status=503)

        appid = str(payload.get("appid") or payload.get("priority_appid") or "").strip() or None
        background = self._payload_bool(payload, "background", True)
        awaitable = cog.run_sync_once(mode="n8n", priority_appid=appid)
        if background:
            task = asyncio.create_task(self._run_n8n_background("opendir_sync", awaitable), name="n8n-opendir-sync")
            return self._n8n_accepted_response("opendir_sync", task)
        summary = await awaitable
        return self._n8n_summary_response("opendir_sync", summary)

    async def handle_n8n_db_backup(self, request):
        auth_error = self._n8n_auth_error(request)
        if auth_error is not None:
            return auth_error
        maintenance_error = self._n8n_maintenance_error()
        if maintenance_error is not None:
            return maintenance_error

        payload = await self._n8n_payload(request)
        cog = self.get_cog("GitHubDBBackupCog")
        if not cog:
            return web.json_response({"ok": False, "error": "GitHubDBBackupCog is not loaded"}, status=503)

        force = self._payload_bool(payload, "force", True)
        background = self._payload_bool(payload, "background", True)
        awaitable = cog.run_backup(force=force, reason="n8n")
        if background:
            task = asyncio.create_task(self._run_n8n_background("db_backup", awaitable), name="n8n-db-backup")
            return self._n8n_accepted_response("db_backup", task)
        result = await awaitable
        return self._n8n_summary_response("db_backup", result)

    async def handle_n8n_steam_db_sync(self, request):
        auth_error = self._n8n_auth_error(request)
        if auth_error is not None:
            return auth_error
        maintenance_error = self._n8n_maintenance_error()
        if maintenance_error is not None:
            return maintenance_error

        payload = await self._n8n_payload(request)
        cog = self.get_cog("SteamDbSyncCommands")
        if not cog:
            return web.json_response({"ok": False, "error": "SteamDbSyncCommands cog is not loaded"}, status=503)

        async def run_sync():
            summary = await cog._run_threaded(
                apply_changes=self._payload_bool(payload, "apply", False),
                include_new=self._payload_bool(payload, "include_new", True),
                max_new=self._payload_int(payload, "max_new", 0),
                max_updates=self._payload_int(payload, "max_updates", 0),
            )
            await cog._alert_if_needed(summary, automatic=False)
            return summary

        background = self._payload_bool(payload, "background", True)
        awaitable = run_sync()
        if background:
            task = asyncio.create_task(self._run_n8n_background("steam_db_sync", awaitable), name="n8n-steam-db-sync")
            return self._n8n_accepted_response("steam_db_sync", task)
        summary = await awaitable
        return self._n8n_summary_response("steam_db_sync", summary)

    async def handle_n8n_r2_maintenance(self, request):
        auth_error = self._n8n_auth_error(request)
        if auth_error is not None:
            return auth_error
        maintenance_error = self._n8n_maintenance_error()
        if maintenance_error is not None:
            return maintenance_error

        payload = await self._n8n_payload(request)
        cog = self.get_cog("R2MaintenanceCommands")
        if not cog:
            return web.json_response({"ok": False, "error": "R2MaintenanceCommands cog is not loaded"}, status=503)

        async def run_maintenance():
            summary = await cog._run_threaded(
                apply_changes=self._payload_bool(payload, "apply", False),
                prefix=str(payload.get("prefix") or bot_config.R2_MAINTENANCE_PREFIX),
                limit=self._payload_int(payload, "limit", bot_config.R2_MAINTENANCE_MAX_OBJECTS, maximum=1000000),
                rename_objects=self._payload_bool(payload, "rename_objects", bot_config.R2_MAINTENANCE_RENAME_OBJECTS),
                clean_lua=self._payload_bool(payload, "clean_lua", bot_config.R2_MAINTENANCE_CLEAN_LUA_COMMENTS),
                use_steam=self._payload_bool(payload, "use_steam", bot_config.R2_MAINTENANCE_STEAM_LOOKUPS),
                max_steam_lookups=self._payload_int(
                    payload,
                    "max_steam_lookups",
                    bot_config.R2_MAINTENANCE_MAX_STEAM_LOOKUPS,
                ),
                use_queue=self._payload_bool(payload, "use_queue", bot_config.R2_MAINTENANCE_QUEUE_ENABLED),
                fallback_to_appid=self._payload_bool(
                    payload,
                    "fallback_to_appid",
                    bot_config.R2_MAINTENANCE_FALLBACK_TO_APPID,
                ),
                ignore_blacklist=self._payload_bool(payload, "ignore_blacklist", False),
            )
            await cog._alert_if_needed(summary, automatic=False)
            return summary

        background = self._payload_bool(payload, "background", True)
        awaitable = run_maintenance()
        if background:
            task = asyncio.create_task(self._run_n8n_background("r2_maintenance", awaitable), name="n8n-r2-maintenance")
            return self._n8n_accepted_response("r2_maintenance", task)
        summary = await awaitable
        return self._n8n_summary_response("r2_maintenance", summary)

    async def handle_terms(self, request):
        return web.Response(text=TERMS_HTML, content_type="text/html")

    async def handle_privacy(self, request):
        return web.Response(text=PRIVACY_HTML, content_type="text/html")

    async def handle_download(self, request):
        """
        Redirect ke R2 setelah validasi JWT.
        FIX: Baca r2_key dari JWT payload (disimpan oleh _find_download).
             Kalau tidak ada, fallback coba semua pola.
        """
        appid = request.match_info.get("appid")
        token = request.query.get("token")

        if not token:
            return web.Response(text="❌ Token tidak ditemukan.", status=401)

        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            if str(payload.get("app_id")) != str(appid):
                return web.Response(text="❌ Token tidak valid untuk game ini.", status=403)
        except jwt.ExpiredSignatureError:
            return web.Response(
                text="❌ Link sudah kadaluarsa. Silakan request ulang di Discord.", status=403
            )
        except jwt.InvalidTokenError:
            return web.Response(text="❌ Token tidak valid.", status=403)

        # Ambil r2_key yang sudah diverifikasi dari JWT, atau fallback ke semua pola
        r2_key = payload.get("r2_key")
        game = self.db.get_game(appid) if getattr(self, "db", None) else None
        game_name = game.get("name") if game else None
        keys_to_try = [r2_key] if r2_key else build_r2_key_candidates(appid, game_name)

        for key in keys_to_try:
            real_url = await generate_presigned_url(key)
            if real_url:
                raise web.HTTPFound(real_url)

        self.record_ai_event(
            "warning",
            "download",
            "Download file was not found in R2.",
            {"appid": str(appid), "candidate_count": len(keys_to_try)},
        )
        self.queue_ai_caretaker(
            "download-file-missing",
            {"appid": str(appid), "candidate_count": len(keys_to_try)},
        )

        return web.Response(text="❌ File tidak ditemukan di storage.", status=404)

    async def notify_admins(self, *args, skip_n8n: bool = False, **kwargs) -> int:
        try:
            title = str(args[0] if args else kwargs.get("title", "admin alert"))
            description = str(args[1] if len(args) > 1 else kwargs.get("description", ""))
            legacy_text = f"{title}\n{description}".lower()
            if "opendir" in legacy_text and "games.json" in legacy_text:
                log.warning("Suppressed legacy games.json OpenDir notification before recording event: %s", title)
                return 0
            self.record_ai_event(
                kwargs.get("level", "warning"),
                "admin_alert",
                f"{title}: {description}",
                kwargs.get("fields"),
            )
            if bot_config.N8N_FORWARD_ADMIN_ALERTS and not skip_n8n:
                asyncio.create_task(
                    self.emit_n8n_event(
                        "admin_alert",
                        {
                            "title": title,
                            "description": description,
                            "level": kwargs.get("level", "warning"),
                            "fields": kwargs.get("fields") or {},
                            "key": kwargs.get("key"),
                        },
                    ),
                    name="n8n-admin-alert",
                )
        except Exception:
            pass
        return await self.notifier.send(*args, **kwargs)

    def record_ai_event(self, level: str, source: str, message: str, fields: dict | None = None) -> None:
        if getattr(self, "ai_events", None):
            self.ai_events.append(level=level, source=source, message=message, fields=fields)

    def queue_ai_caretaker(self, reason: str, context: dict | None = None, *, force: bool = False) -> None:
        caretaker = getattr(self, "ai_caretaker", None)
        if not caretaker:
            return
        asyncio.create_task(caretaker.trigger(reason, context=sanitize_data(context or {}), force=force))

    def _prune_slash_commands(self) -> None:
        """Remove every app command except the public allowlist.

        Decorated admin commands may still exist inside cogs so their background
        jobs can run, but they must not be published to Discord's slash command UI.
        """
        for command in list(self.tree.get_commands()):
            if command.name not in PUBLIC_SLASH_COMMAND_ALLOWLIST:
                command_type = getattr(command, "type", discord.AppCommandType.chat_input)
                removed = self.tree.remove_command(command.name, type=command_type)
                if removed:
                    log.info("🧹 Hidden slash command: /%s", command.name)

    async def sync_minimal_commands_for_guilds(self) -> None:
        """Force each guild command list to mirror the global allowlist.

        Global slash command changes can take time to disappear in DMs, but guild
        commands update quickly. This prevents old admin commands from staying
        visible in server command pickers after a deploy.
        """
        self._prune_slash_commands()
        for guild in list(self.guilds):
            try:
                self.tree.clear_commands(guild=guild)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "🧹 Guild slash commands reset for %s: %s",
                    guild.name,
                    ", ".join(cmd.name for cmd in synced) or "none",
                )
            except Exception as exc:
                log.warning("Failed to reset slash commands for guild %s: %r", guild, exc)

    async def load_cogs(self):
        cogs_dir = Path(__file__).parent / "cogs"
        if not cogs_dir.exists():
            log.warning("Cogs directory not found")
            return
        for cog_file in cogs_dir.glob("*.py"):
            if cog_file.name.startswith("_"):
                continue
            cog_name = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(cog_name)
                log.info(f"✅ Loaded cog: {cog_name}")
            except Exception as e:
                log.error(f"Failed to load cog {cog_name}: {e}")

    async def close(self):
        log.info("🛑 Shutting down...")
        if hasattr(self, "db"):
            self.db.save()
        if self.session and not self.session.closed:
            await self.session.close()
        if getattr(self, "_ai_log_handler", None):
            logging.getLogger().removeHandler(self._ai_log_handler)
        await super().close()

    async def on_ready(self):
        log.info("=" * 60)
        log.info(f"🤖 Bot: {self.user}  |  v{self.version}")
        log.info("=" * 60)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name="/gen to generate game")
        )

        # setup_hook may run before guild cache is populated. Do the cleanup again
        # once Discord has provided the guild list.
        asyncio.create_task(self.sync_minimal_commands_for_guilds())

        if not self._ready_notified:
            self._ready_notified = True
            deploy_info = _deployment_info()
            await self.notify_admins(
                "HORIZON BOT is online",
                "Bot started successfully and is ready to receive commands.",
                level="info",
                fields={
                    "Version": self.version,
                    "Commit": deploy_info.get("commit_short") or "-",
                    "Railway service": deploy_info.get("railway_service") or "-",
                    "Railway deployment": deploy_info.get("railway_deployment_id") or "-",
                    "Guilds": str(len(self.guilds)),
                    "Health endpoint": "/health",
                },
                key="bot-ready",
                force=True,
            )

    async def on_guild_join(self, guild):
        log.info(f"➕ Joined guild: {guild.name}")


    async def on_app_command_error(self, interaction: discord.Interaction, error):
        error_str = str(error)
        command_name = getattr(getattr(interaction, "command", None), "qualified_name", "unknown")

        if "Interaction has already been acknowledged" in error_str or "Unknown interaction" in error_str:
            if interaction.response.is_done():
                return

        # Jika ini adalah error 'not found' yang sudah ditangani secara visual di /gen, jangan lapor admin
        if "NotFound" in error_str and command_name == "gen":
            return
        user_text = f"{interaction.user} ({interaction.user.id})" if interaction.user else "unknown"
        guild_text = f"{interaction.guild} ({interaction.guild.id})" if interaction.guild else "DM"

        log.error("Slash command error in %s: %s", command_name, error, exc_info=error)
        await self.notify_admins(
            "Slash command error",
            f"Command `/{command_name}` failed.",
            level="error",
            fields={
                "User": user_text,
                "Guild": guild_text,
                "Error": repr(error)[:1000],
            },
            key=f"slash-error-{command_name}-{type(error).__name__}",
        )
        self.queue_ai_caretaker(
            "slash-command-error",
            {
                "command": command_name,
                "user": user_text,
                "guild": guild_text,
                "error": repr(error)[:1000],
            },
            force=True,
        )

        embed = discord.Embed(
            title="Command failed",
            description="Something went wrong while processing this command. The admin has been notified.",
            color=0xE74C3C,
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    async def on_error(self, event_method, *args, **kwargs):
        trace = traceback.format_exc()
        log.error("Unhandled Discord event error in %s:\n%s", event_method, trace)
        await self.notify_admins(
            "Unhandled bot event error",
            f"Event `{event_method}` failed.",
            level="error",
            fields={"Traceback": trace[-1000:]},
            key=f"event-error-{event_method}",
        )
        self.queue_ai_caretaker(
            "unhandled-event-error",
            {"event": event_method, "traceback_tail": trace[-1000:]},
            force=True,
        )


async def main():
    bot = SteamBot()
    try:
        log.info("🚀 Starting bot...")
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("Bot crashed during startup/runtime")
        raise
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    else:
        try:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            pass

    exit_code = 0
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception:
        exit_code = 1
    sys.exit(exit_code)
