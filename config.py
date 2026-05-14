"""
Configuration management for Discord Bot
All environment variables and constants are defined here
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Base paths
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache_games"
FILES_DIR = BASE_DIR / "Files"

# Create directories if they don't exist
for directory in [DATA_DIR, LOGS_DIR, CACHE_DIR, FILES_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

# Discord configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not found in environment variables")

# Admin configuration
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "562612184333680709").split(",") if x.strip()]

# API URLs
R2_BASE_URL = os.getenv("R2_BASE_URL", "")
ADMIN_WEBHOOK = os.getenv("ADMIN_WEBHOOK", "")
WEB_URL = os.getenv("WEB_URL", "http://localhost:8080").rstrip('/')
JWT_SECRET = os.getenv("JWT_SECRET", "super-secret-key")
PORT = int(os.getenv("PORT", "8080"))

# R2 presigned-URL credentials (optional – enables expiring download links)
R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID", "")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "")
LINK_EXPIRE_SECONDS  = int(os.getenv("LINK_EXPIRE_SECONDS", "3600"))  # 1 hour

# Default Steam store country code for regional pricing.
# Uses the user's Discord locale when it can be mapped; falls back to this.
# See: https://store.steampowered.com/api/appdetails?appids=1&cc=id
DEFAULT_CC = os.getenv("DEFAULT_CC", "id")

# Steam API
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_STORE_API = "https://store.steampowered.com/api/appdetails"
STEAM_SEARCH_API = "https://store.steampowered.com/api/storesearch"

# GitHub API
GITHUB_API_BASE = "https://api.github.com/repos/SteamAutoCracks/ManifestHub"
GITHUB_BRANCHES_URL = f"{GITHUB_API_BASE}/branches"
MANIFESTHUB_PATH = os.getenv("MANIFESTHUB_PATH", "SteamAutoCracks/ManifestHub")

# Database files
DB_PATH = DATA_DIR / "games.json"
BACKFILL_STATE = DATA_DIR / "backfill_state.json"
CRAWLER_STATE = DATA_DIR / "crawler_state.json"
CACHE_FILE = DATA_DIR / "cache.json"

# Logging configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = LOGS_DIR / "bot.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Bot settings
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
BOT_VERSION = "9.0.0"
BOT_DESCRIPTION = "Steam Game Database & Download Manager"

# Download settings
MAX_DOWNLOAD_SIZE_MB = int(os.getenv("MAX_DOWNLOAD_SIZE_MB", "10240"))  # 10GB default
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))  # 5 minutes
CACHE_DAYS = int(os.getenv("CACHE_DAYS", "14"))

# Rate limiting
RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "3600"))  # 1 hour

# Feature flags
ENABLE_AUTO_BACKUP = os.getenv("ENABLE_AUTO_BACKUP", "true").lower() == "true"
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
MAX_BACKUPS = int(os.getenv("MAX_BACKUPS", "5"))

# Crawling settings
CRAWL_INTERVAL_HOURS = int(os.getenv("CRAWL_INTERVAL_HOURS", "6"))
BACKFILL_START_ID = int(os.getenv("BACKFILL_START_ID", "10"))
GITHUB_PAGES_PER_SYNC = int(os.getenv("GITHUB_PAGES_PER_SYNC", "10"))

# Embed colors (in decimal)
COLOR_SUCCESS = 0x2ECC71  # Green
COLOR_ERROR = 0xE74C3C    # Red
COLOR_INFO = 0x3498DB     # Blue
COLOR_WARNING = 0xF39C12  # Orange
COLOR_DOWNLOAD = 0x1ABC9C # Teal

# Validation
def validate_config():
    """Validate critical configuration"""
    errors = []
    
    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN is required")
    
    if errors:
        raise ValueError(f"Configuration errors: {', '.join(errors)}")

# Run validation on import
validate_config()
