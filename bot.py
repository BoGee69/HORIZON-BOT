"""
Steam Game Database & Download Manager Bot
"""
import asyncio
import logging
import sys
import traceback
from pathlib import Path

import discord
from discord.ext import commands
import aiohttp
from aiohttp import web
import jwt

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    DISCORD_TOKEN, BOT_PREFIX, BOT_VERSION, BOT_DESCRIPTION,
    LOG_LEVEL, LOG_FILE, LOG_FORMAT, LOG_DATE_FORMAT,
    JWT_SECRET, PORT,
)
from utils.alerts import AdminNotifier
from utils.database import DatabaseManager
from utils.diagnostics import collect_health
from utils.r2_presign import generate_presigned_url

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# Fallback patterns jika r2_key tidak ada di JWT (backward compat)
R2_KEY_PATTERNS = [
    "Database/{appid}.zip",
    "Database/[{appid}].zip",
    "[{appid}].zip",
    "{appid}.zip",
]


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
        self._ready_notified = False

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        log.info("✅ HTTP session created")
        self.db.load()
        await self.load_cogs()
        self.tree.on_error = self.on_app_command_error

        # FIX: asyncio.create_task() — self.loop deprecated di discord.py 2.x
        asyncio.create_task(self.start_web_server())

        try:
            synced = await self.tree.sync()
            log.info(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")

    async def start_web_server(self):
        app = web.Application()
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/download/{appid}", self.handle_download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info(f"🌐 Web API running on port {PORT}")

    async def handle_health(self, request):
        health = await collect_health(self)
        return web.json_response(health, status=200 if health["ok"] else 503)

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
        keys_to_try = [r2_key] if r2_key else [p.format(appid=appid) for p in R2_KEY_PATTERNS]

        for key in keys_to_try:
            real_url = await generate_presigned_url(key)
            if real_url:
                raise web.HTTPFound(real_url)

        return web.Response(text="❌ File tidak ditemukan di storage.", status=404)

    async def notify_admins(self, *args, **kwargs) -> int:
        return await self.notifier.send(*args, **kwargs)

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
        await super().close()

    async def on_ready(self):
        log.info("=" * 60)
        log.info(f"🤖 Bot: {self.user}  |  v{self.version}")
        log.info("=" * 60)
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.playing, name="/gen to generate game")
        )

        if not self._ready_notified:
            self._ready_notified = True
            await self.notify_admins(
                "triadbot is online",
                "Bot started successfully and is ready to receive commands.",
                level="info",
                fields={
                    "Version": self.version,
                    "Guilds": str(len(self.guilds)),
                    "Health endpoint": "/health",
                },
                key="bot-ready",
                force=True,
            )

    async def on_guild_join(self, guild):
        log.info(f"➕ Joined guild: {guild.name}")


    async def on_app_command_error(self, interaction: discord.Interaction, error):
        command_name = getattr(getattr(interaction, "command", None), "qualified_name", "unknown")
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


async def main():
    bot = SteamBot()
    try:
        log.info("🚀 Starting bot...")
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        pass
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        sys.exit(0)
