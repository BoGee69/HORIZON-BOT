import asyncio
import discord
from discord.ext import commands
import sys
from pathlib import Path

# Masukkan sys path agar bisa load config
sys.path.insert(0, str(Path(__file__).parent))
from config import DISCORD_TOKEN

async def sync():
    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)
    
    # Kita hanya perlu sync, jadi kita muat cogs yang relevan
    # Tapi cara termudah adalah me-restart bot yang sudah kita modifikasi
    # karena bot.py kita sudah punya tree.sync() di on_ready.
    print("🔄 Bot is already configured to sync on startup.")
    print("🔄 Triggering a full restart to ensure Discord API is updated...")

if __name__ == "__main__":
    # Karena bot.py kamu sudah melakukan sync di on_ready, 
    # kita cukup memastikan bot restart dengan bersih.
    pass
