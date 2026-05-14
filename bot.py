"""
Steam Game Database & Download Manager Bot
Main bot entry point with cog loading and event handling
"""
import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
import aiohttp
from aiohttp import web
import jwt

# Setup path for imports
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    DISCORD_TOKEN, BOT_PREFIX, BOT_VERSION, BOT_DESCRIPTION,
    LOG_LEVEL, LOG_FILE, LOG_FORMAT, LOG_DATE_FORMAT,
    JWT_SECRET, PORT
)
from utils.database import DatabaseManager
from utils.r2_presign import generate_presigned_url

# Setup logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format=LOG_FORMAT,
    datefmt=LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding='utf-8')
    ]
)

logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('discord.http').setLevel(logging.WARNING)

log = logging.getLogger(__name__)


class SteamBot(commands.Bot):
    """Main bot class with enhanced functionality"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix=BOT_PREFIX,
            intents=intents,
            description=BOT_DESCRIPTION
        )
        
        self.version = BOT_VERSION
        self.start_time = discord.utils.utcnow() 
        self.session: aiohttp.ClientSession | None = None
        self.db = DatabaseManager()
        self.is_ready = False
    
    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        log.info("✅ HTTP session created")
        
        self.db.load()
        await self.load_cogs()
        
        # 🌐 NYALAKAN WEB SERVER UNTUK DOWNLOAD LINK (FUNGSI BARU)
        self.loop.create_task(self.start_web_server())
        
        try:
            synced = await self.tree.sync()
            log.info(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")

    async def start_web_server(self):
        """Menjalankan Web API pendamping untuk sistem download JWT"""
        app = web.Application()
        app.router.add_get('/download/{appid}', self.handle_download)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        log.info(f"🌐 Web API running on port {PORT}")

    async def handle_download(self, request):
        """Endpoint ini menangani klik link dari user dan mengecek Token JWT"""
        appid = request.match_info.get('appid')
        token = request.query.get('token')
        
        if not token:
            return web.Response(text="❌ Token tidak ditemukan / Missing token.", status=401)
            
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            if str(payload.get("app_id")) != str(appid):
                return web.Response(text="❌ Token tidak valid untuk game ini.", status=403)
                
        except jwt.ExpiredSignatureError:
            return web.Response(text="❌ Link sudah kadaluarsa (Expired). Silakan request link baru di Discord.", status=403)
        except jwt.InvalidTokenError:
            return web.Response(text="❌ Token rusak / Invalid token.", status=403)
            
        # Token valid -> Generate link asli R2 secara rahasia
        real_url = await generate_presigned_url(appid)
        
        if not real_url:
            return web.Response(text="❌ Server sedang gangguan. Gagal menghubungi Cloudflare.", status=500)
            
        # Redirect IDM/Browser user ke link asli
        raise web.HTTPFound(real_url)

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
        log.info("🛑 Shutting down bot...")
        if hasattr(self, 'db'):
            self.db.save()
            log.info("💾 Database saved")
        
        if self.session and not self.session.closed:
            await self.session.close()
            log.info("✅ HTTP session closed")
        
        await super().close()
    
    async def on_ready(self):
        self.is_ready = True
        log.info("=" * 60)
        log.info(f"🤖 Bot: {self.user}")
        log.info(f"📦 Version: {self.version}")
        log.info("=" * 60)
        
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="/gen to generate game"
            )
        )
    
    async def on_guild_join(self, guild):
        log.info(f"➕ Joined guild: {guild.name}")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name="/gen to generate game"
            )
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