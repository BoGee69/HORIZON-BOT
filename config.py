"""
Configuration management for Discord Bot
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

DEFAULT_WEB_URL = "https://triadgames.up.railway.app"


def parse_id_list(value: str) -> list[int]:
    ids = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            ids.append(int(item))
    return ids


def parse_id_set(value: str) -> set[int]:
    return set(parse_id_list(value))


def parse_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    path = Path(raw) if raw else default
    if not path.is_absolute():
        path = Path(__file__).parent / path
    return path


BASE_DIR  = Path(__file__).parent
DATA_DIR  = BASE_DIR / "data"
LOGS_DIR  = BASE_DIR / "logs"
CACHE_DIR = DATA_DIR / "cache_games"
FILES_DIR = BASE_DIR / "Files"

for directory in [DATA_DIR, LOGS_DIR, CACHE_DIR, FILES_DIR]:
    directory.mkdir(parents=True, exist_ok=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not found in environment variables")

ADMIN_IDS = parse_id_list(os.getenv("ADMIN_IDS", "562612184333680709"))
ADMIN_ALERT_IDS = parse_id_list(os.getenv("ADMIN_ALERT_IDS", "")) or ADMIN_IDS
ADMIN_ALERT_COOLDOWN_SECONDS = int(os.getenv("ADMIN_ALERT_COOLDOWN_SECONDS", "300"))
ALERT_ON_LIMIT_HIT = parse_bool(os.getenv("ALERT_ON_LIMIT_HIT", "true"), True)
ADMIN_ROLE_IDS = parse_id_set(os.getenv("ADMIN_ROLE_IDS", ""))
ADMIN_ROLE_NAMES = {
    x.strip().lower()
    for x in os.getenv("ADMIN_ROLE_NAMES", "admin,administrator,owner").split(",")
    if x.strip()
}
DONOR_ROLE_IDS = parse_id_set(os.getenv("DONOR_ROLE_IDS", ""))
DONOR_ROLE_NAMES = {
    x.strip().lower()
    for x in os.getenv("DONOR_ROLE_NAMES", "donor").split(",")
    if x.strip()
}
BOOSTER_ROLE_NAME = os.getenv("BOOSTER_ROLE_NAME", "Booster").strip() or "Booster"
BOOSTER_ROLE_IDS = parse_id_set(
    ",".join(
        item
        for item in [
            os.getenv("BOOSTER_ROLE_ID", "").strip(),
            os.getenv("BOOSTER_ROLE_IDS", "").strip(),
        ]
        if item
    )
)
BOOSTER_ROLE_NAMES = {
    x.strip().lower()
    for x in os.getenv("BOOSTER_ROLE_NAMES", BOOSTER_ROLE_NAME).split(",")
    if x.strip()
}
ADMIN_WEBHOOK = os.getenv("ADMIN_WEBHOOK", "")
DEFAULT_CC    = os.getenv("DEFAULT_CC", "id")

R2_BASE_URL = os.getenv("R2_BASE_URL", "")

# FIX 3: WEB_URL — WAJIB di-set di Railway env agar JWT link tidak nunjuk ke localhost.
# Isi dengan URL Railway kamu, e.g. https://triadbot-production.up.railway.app
WEB_URL    = os.getenv("WEB_URL", DEFAULT_WEB_URL).rstrip("/")
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
PORT       = int(os.getenv("PORT", "8080"))

R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID", "")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "")
LINK_EXPIRE_SECONDS  = int(os.getenv("LINK_EXPIRE_SECONDS", "3600"))

STEAM_API_KEY    = os.getenv("STEAM_API_KEY", "")
STEAM_STORE_API  = "https://store.steampowered.com/api/appdetails"
STEAM_SEARCH_API = "https://store.steampowered.com/api/storesearch"

GITHUB_API_BASE      = "https://api.github.com/repos/SteamAutoCracks/ManifestHub"
GITHUB_BRANCHES_URL  = f"{GITHUB_API_BASE}/branches"
MANIFESTHUB_PATH     = os.getenv("MANIFESTHUB_PATH", "SteamAutoCracks/ManifestHub")

DB_PATH         = DATA_DIR / "games.json"
GEN_USAGE_PATH  = env_path("GEN_USAGE_PATH", DATA_DIR / "gen_usage.json")
BACKFILL_STATE  = DATA_DIR / "backfill_state.json"
CRAWLER_STATE   = DATA_DIR / "crawler_state.json"
CACHE_FILE      = DATA_DIR / "cache.json"

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE        = LOGS_DIR / "bot.log"
LOG_FORMAT      = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

BOT_PREFIX      = os.getenv("BOT_PREFIX", "!")
BOT_VERSION     = "9.1.0"
BOT_DESCRIPTION = "Steam Game Database & Download Manager"

MAX_DOWNLOAD_SIZE_MB   = int(os.getenv("MAX_DOWNLOAD_SIZE_MB", "10240"))
DOWNLOAD_TIMEOUT       = int(os.getenv("DOWNLOAD_TIMEOUT", "300"))
CACHE_DAYS             = int(os.getenv("CACHE_DAYS", "14"))

RATE_LIMIT_REQUESTS = int(os.getenv("RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW   = int(os.getenv("RATE_LIMIT_WINDOW", "3600"))
GEN_DAILY_LIMIT     = int(os.getenv("GEN_DAILY_LIMIT", "10"))

ENABLE_AUTO_BACKUP    = os.getenv("ENABLE_AUTO_BACKUP", "true").lower() == "true"
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
MAX_BACKUPS           = int(os.getenv("MAX_BACKUPS", "5"))

CRAWL_INTERVAL_HOURS  = int(os.getenv("CRAWL_INTERVAL_HOURS", "6"))
BACKFILL_START_ID     = int(os.getenv("BACKFILL_START_ID", "10"))
GITHUB_PAGES_PER_SYNC = int(os.getenv("GITHUB_PAGES_PER_SYNC", "10"))

COLOR_SUCCESS  = 0x2ECC71
COLOR_ERROR    = 0xE74C3C
COLOR_INFO     = 0x3498DB
COLOR_WARNING  = 0xF39C12
COLOR_DOWNLOAD = 0x1ABC9C


def validate_config():
    errors = []
    if not DISCORD_TOKEN:
        errors.append("DISCORD_TOKEN is required")
    if not JWT_SECRET or JWT_SECRET == "super-secret-key" or len(JWT_SECRET) < 32:
        errors.append("JWT_SECRET must be set to a random value with at least 32 characters")
    # FIX 3: Peringatan kalau WEB_URL kosong — JWT link tidak akan berfungsi
    if not WEB_URL:
        import logging
        logging.getLogger(__name__).warning(
            "⚠️  WEB_URL tidak di-set! Download link via JWT akan mengarah ke localhost. "
            "Set WEB_URL=https://<nama-app>.up.railway.app di Railway environment variables."
        )
    if errors:
        raise ValueError(f"Configuration errors: {', '.join(errors)}")


validate_config()
