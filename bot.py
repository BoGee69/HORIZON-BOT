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

# Setup path for imports
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    DISCORD_TOKEN, BOT_PREFIX, BOT_VERSION, BOT_DESCRIPTION,
    LOG_LEVEL, LOG_FILE, LOG_FORMAT, LOG_DATE_FORMAT
)
from utils.database import DatabaseManager

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

# Reduce discord.py logging noise
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('discord.http').setLevel(logging.WARNING)

log = logging.getLogger(__name__)


class SteamBot(commands.Bot):
    """Main bot class with enhanced functionality"""
    
    def __init__(self):
        # Setup intents
        intents = discord.Intents.default()
        intents.message_content = True
        
        super().__init__(
            command_prefix=BOT_PREFIX,
            intents=intents,
            description=BOT_DESCRIPTION
        )
        
        # Bot metadata
        self.version = BOT_VERSION
        # FIX: Gunakan discord.utils.utcnow() agar format timezone sama dengan command status
        self.start_time = discord.utils.utcnow() 
        
        # HTTP session for API calls
        self.session: aiohttp.ClientSession | None = None
        
        # Database manager
        self.db = DatabaseManager()
        
        # Status tracking
        self.is_ready = False
    
    async def setup_hook(self):
        """
        Called before bot connects to Discord
        Setup HTTP session and load cogs
        """
        # Create HTTP session
        self.session = aiohttp.ClientSession()
        log.info("✅ HTTP session created")
        
        # Load database
        self.db.load()
        
        # Load all cogs
        await self.load_cogs()
        
        # Sync commands
        try:
            synced = await self.tree.sync()
            log.info(f"✅ Synced {len(synced)} slash commands")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")
    
    async def load_cogs(self):
        """Load all cogs from cogs directory"""
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
        """
        Cleanup when bot shuts down
        Close HTTP session and save database
        """
        log.info("🛑 Shutting down bot...")
        
        # Save database
        if hasattr(self, 'db'):
            self.db.save()
            log.info("💾 Database saved")
        
        # Close HTTP session
        if self.session and not self.session.closed:
            await self.session.close()
            log.info("✅ HTTP session closed")
        
        await super().close()
    
    async def on_ready(self):
        """Called when bot is ready and connected to Discord"""
        self.is_ready = True
        
        log.info("=" * 60)
        log.info(f"🤖 Bot: {self.user}")
        log.info(f"🆔 ID: {self.user.id}")
        log.info(f"📊 Guilds: {len(self.guilds)}")
        log.info(f"📦 Version: {self.version}")
        log.info(f"💾 Database: {len(self.db.game_db):,} games loaded")
        log.info("=" * 60)
        
        # Set bot activity
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.db.game_db):,} games | /gen to search"
            )
        )
    
    async def on_command_error(self, ctx, error):
        """Global error handler for prefix commands"""
        if isinstance(error, commands.CommandNotFound):
            return
        
        log.error(f"Command error: {error}", exc_info=error)
    
    async def on_app_command_error(
        self, 
        interaction: discord.Interaction, 
        error: discord.app_commands.AppCommandError
    ):
        """Global error handler for slash commands"""
        log.error(f"App command error: {error}", exc_info=error)
        
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"❌ An error occurred: {str(error)}",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"❌ An error occurred: {str(error)}",
                ephemeral=True
            )
    
    async def on_guild_join(self, guild):
        """Called when bot joins a new guild"""
        log.info(f"➕ Joined guild: {guild.name} (ID: {guild.id})")
        
        # Update activity
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{len(self.guilds)} servers | /gen to search"
            )
        )
    
    async def on_guild_remove(self, guild):
        """Called when bot leaves a guild"""
        log.info(f"➖ Left guild: {guild.name} (ID: {guild.id})")


async def main():
    """Main entry point"""
    bot = SteamBot()
    
    try:
        log.info("🚀 Starting bot...")
        await bot.start(DISCORD_TOKEN)
        
    except KeyboardInterrupt:
        log.info("⌨️ Keyboard interrupt received")
    except Exception as e:
        log.critical(f"💥 Fatal error: {e}", exc_info=True)
    finally:
        if not bot.is_closed():
            await bot.close()
        log.info("👋 Bot stopped")


if __name__ == "__main__":
    # Windows-specific event loop policy
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        sys.exit(0)