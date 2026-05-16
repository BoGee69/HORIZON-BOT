"""
Steam Game Database & Download Manager Bot
"""
import asyncio
import logging
import sys
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
from utils.database import DatabaseManager
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
        super().__init__(command_prefix=BOT_PREFIX, intents=intents, description=BOT_DESCRIPTION)
        self.version    = BOT_VERSION
        self.start_time = discord.utils.utcnow()
        self.session: aiohttp.ClientSession | None = None
        self.db         = DatabaseManager()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        log.info("✅ HTTP session created")
        self.db.load()
        await self.load_cogs()

        # FIX: asyncio.create_task() — self.loop deprecated di discord.py 2.x
        asyncio.create_task(self.start_web_server())

        try:
            synced = await self.tree.sync()
            log.info(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")

    async def start_web_server(self):
        app = web.Application()
        app.router.add_get("/download/{appid}", self.handle_download)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info(f"🌐 Web API running on port {PORT}")

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

    async def on_guild_join(self, guild):
        log.info(f"➕ Joined guild: {guild.name}")


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
