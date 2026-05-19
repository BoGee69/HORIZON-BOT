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


def parse_csv_set(value: str, default: str = "") -> set[str]:
    raw = value if value is not None else default
    return {
        item.strip().lower().lstrip(".")
        for item in raw.split(",")
        if item.strip()
    }


def parse_str_list(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


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
MODERATOR_ROLE_IDS = parse_id_set(os.getenv("MODERATOR_ROLE_IDS", ""))
MODERATOR_ROLE_NAMES = {
    x.strip().lower()
    for x in os.getenv("MODERATOR_ROLE_NAMES", "triadbot,admin,moderator,mod").split(",")
    if x.strip()
}
MODERATOR_ROLE_REQUIRED = parse_bool(os.getenv("MODERATOR_ROLE_REQUIRED", "false"), False)
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

R2_MAINTENANCE_ENABLED = parse_bool(os.getenv("R2_MAINTENANCE_ENABLED", "false"), False)
R2_MAINTENANCE_APPLY = parse_bool(os.getenv("R2_MAINTENANCE_APPLY", "false"), False)
R2_MAINTENANCE_RUN_ON_START = parse_bool(os.getenv("R2_MAINTENANCE_RUN_ON_START", "true"), True)
R2_MAINTENANCE_START_DELAY_SECONDS = float(os.getenv("R2_MAINTENANCE_START_DELAY_SECONDS", "10"))
R2_MAINTENANCE_INTERVAL_HOURS = float(os.getenv("R2_MAINTENANCE_INTERVAL_HOURS", "24"))
R2_MAINTENANCE_PREFIX = os.getenv("R2_MAINTENANCE_PREFIX", "Database/")
R2_MAINTENANCE_MAX_OBJECTS = int(os.getenv("R2_MAINTENANCE_MAX_OBJECTS", "100"))
R2_MAINTENANCE_MAX_ZIP_MB = int(os.getenv("R2_MAINTENANCE_MAX_ZIP_MB", "50"))
R2_MAINTENANCE_RENAME_OBJECTS = parse_bool(os.getenv("R2_MAINTENANCE_RENAME_OBJECTS", "true"), True)
R2_MAINTENANCE_CLEAN_COMMENTS = parse_bool(
    os.getenv("R2_MAINTENANCE_CLEAN_COMMENTS", os.getenv("R2_MAINTENANCE_CLEAN_LUA_COMMENTS", "true")),
    True,
)
R2_MAINTENANCE_CLEAN_LUA_COMMENTS = R2_MAINTENANCE_CLEAN_COMMENTS
R2_MAINTENANCE_CLEAN_EXTENSIONS = parse_csv_set(
    os.getenv("R2_MAINTENANCE_CLEAN_EXTENSIONS", "lua,manifest,acf,vdf"),
    "lua,manifest,acf,vdf",
)
R2_MAINTENANCE_STEAM_LOOKUPS = parse_bool(os.getenv("R2_MAINTENANCE_STEAM_LOOKUPS", "false"), False)
R2_MAINTENANCE_MAX_STEAM_LOOKUPS = int(os.getenv("R2_MAINTENANCE_MAX_STEAM_LOOKUPS", "25"))
R2_MAINTENANCE_STEAM_DELAY_SECONDS = float(os.getenv("R2_MAINTENANCE_STEAM_DELAY_SECONDS", "0.12"))
R2_MAINTENANCE_QUEUE_ENABLED = parse_bool(os.getenv("R2_MAINTENANCE_QUEUE_ENABLED", "true"), True)
R2_MAINTENANCE_FALLBACK_TO_APPID = parse_bool(os.getenv("R2_MAINTENANCE_FALLBACK_TO_APPID", "true"), True)
R2_MAINTENANCE_BLACKLIST_THRESHOLD = int(os.getenv("R2_MAINTENANCE_BLACKLIST_THRESHOLD", "3"))
R2_MAINTENANCE_STATE_PATH = env_path("R2_MAINTENANCE_STATE_PATH", DATA_DIR / "r2_maintenance_state.json")

STEAM_DB_SYNC_ENABLED = parse_bool(os.getenv("STEAM_DB_SYNC_ENABLED", "false"), False)
STEAM_DB_SYNC_APPLY = parse_bool(os.getenv("STEAM_DB_SYNC_APPLY", "false"), False)
STEAM_DB_SYNC_RUN_ON_START = parse_bool(os.getenv("STEAM_DB_SYNC_RUN_ON_START", "true"), True)
STEAM_DB_SYNC_START_DELAY_SECONDS = float(os.getenv("STEAM_DB_SYNC_START_DELAY_SECONDS", "5"))
STEAM_DB_SYNC_INTERVAL_HOURS = float(os.getenv("STEAM_DB_SYNC_INTERVAL_HOURS", "24"))
STEAM_DB_SYNC_INCLUDE_NEW = parse_bool(os.getenv("STEAM_DB_SYNC_INCLUDE_NEW", "true"), True)
STEAM_DB_SYNC_MAX_NEW = int(os.getenv("STEAM_DB_SYNC_MAX_NEW", "0"))
STEAM_DB_SYNC_MAX_UPDATES = int(os.getenv("STEAM_DB_SYNC_MAX_UPDATES", "0"))
STEAM_DB_SYNC_TIMEOUT_SECONDS = int(os.getenv("STEAM_DB_SYNC_TIMEOUT_SECONDS", "120"))
STEAM_DB_SYNC_PAGE_SIZE = int(os.getenv("STEAM_DB_SYNC_PAGE_SIZE", "50000"))
STEAM_DB_SYNC_INCLUDE_GAMES = parse_bool(os.getenv("STEAM_DB_SYNC_INCLUDE_GAMES", "true"), True)
STEAM_DB_SYNC_INCLUDE_DLC = parse_bool(os.getenv("STEAM_DB_SYNC_INCLUDE_DLC", "true"), True)
STEAM_DB_SYNC_INCLUDE_SOFTWARE = parse_bool(os.getenv("STEAM_DB_SYNC_INCLUDE_SOFTWARE", "false"), False)

AI_MAINTENANCE_ENABLED = parse_bool(os.getenv("AI_MAINTENANCE_ENABLED", "false"), False)
AI_PROVIDER = os.getenv("AI_PROVIDER", "ollama").strip().lower() or "ollama"
AI_MAINTENANCE_PROVIDER = os.getenv("AI_MAINTENANCE_PROVIDER", AI_PROVIDER).strip().lower() or AI_PROVIDER
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "").strip()
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "https://ollama.com").strip().rstrip("/") or "https://ollama.com"
OLLAMA_TIMEOUT_SECONDS = float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180"))
OLLAMA_THINK = os.getenv("OLLAMA_THINK", "medium").strip().lower()
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "5m").strip()


def _default_ai_model(provider: str, *, ollama: str, gemini: str = "gemini-2.5-flash-lite") -> str:
    return ollama if (provider or "").strip().lower() == "ollama" else gemini


# Role-based AI model router.
# GPT stays as the default brain. Specialist models are only helpers for narrow workloads.
AI_MODEL_DEFAULT = os.getenv(
    "AI_MODEL_DEFAULT",
    _default_ai_model(AI_PROVIDER, ollama="gpt-oss:120b-cloud"),
).strip() or _default_ai_model(AI_PROVIDER, ollama="gpt-oss:120b-cloud")
AI_MODEL_FALLBACK = os.getenv(
    "AI_MODEL_FALLBACK",
    _default_ai_model(AI_PROVIDER, ollama="glm-5.1:cloud"),
).strip() or AI_MODEL_DEFAULT
AI_MODEL_CHAT = os.getenv("AI_MODEL_CHAT", AI_MODEL_DEFAULT).strip() or AI_MODEL_DEFAULT
AI_MODEL_BACKGROUND_MONITOR = os.getenv(
    "AI_MODEL_BACKGROUND_MONITOR",
    _default_ai_model(AI_PROVIDER, ollama="qwen3.5:cloud"),
).strip() or AI_MODEL_DEFAULT
AI_MODEL_MONITOR = os.getenv("AI_MODEL_MONITOR", AI_MODEL_BACKGROUND_MONITOR).strip() or AI_MODEL_BACKGROUND_MONITOR
AI_MODEL_OPERATOR = os.getenv("AI_MODEL_OPERATOR", AI_MODEL_DEFAULT).strip() or AI_MODEL_DEFAULT
AI_MODEL_R2_MAINTENANCE = os.getenv("AI_MODEL_R2_MAINTENANCE", AI_MODEL_DEFAULT).strip() or AI_MODEL_DEFAULT
AI_MODEL_INTENT_ROUTER = os.getenv("AI_MODEL_INTENT_ROUTER", AI_MODEL_DEFAULT).strip() or AI_MODEL_DEFAULT
AI_MODEL_PROPOSAL_MANAGER = os.getenv("AI_MODEL_PROPOSAL_MANAGER", AI_MODEL_DEFAULT).strip() or AI_MODEL_DEFAULT
AI_MODEL_CODE_DEBUGGER = os.getenv(
    "AI_MODEL_CODE_DEBUGGER",
    _default_ai_model(AI_PROVIDER, ollama="kimi-k2.6:cloud"),
).strip() or AI_MODEL_DEFAULT
AI_MODEL_GITHUB = os.getenv("AI_MODEL_GITHUB", AI_MODEL_CODE_DEBUGGER).strip() or AI_MODEL_CODE_DEBUGGER
AI_MODEL_SECURITY = os.getenv("AI_MODEL_SECURITY", AI_MODEL_DEFAULT).strip() or AI_MODEL_DEFAULT

AI_MAINTENANCE_MODEL = os.getenv("AI_MAINTENANCE_MODEL", AI_MODEL_MONITOR).strip() or AI_MODEL_MONITOR
AI_MAINTENANCE_INTERVAL_MINUTES = float(os.getenv("AI_MAINTENANCE_INTERVAL_MINUTES", "360"))
AI_MAINTENANCE_ALERT_IDS = parse_id_list(os.getenv("AI_MAINTENANCE_ALERT_IDS", "")) or ADMIN_IDS[:1]
AI_MAINTENANCE_MAX_PROMPT_CHARS = int(os.getenv("AI_MAINTENANCE_MAX_PROMPT_CHARS", "12000"))
AI_MAINTENANCE_DM_ON_OK = parse_bool(os.getenv("AI_MAINTENANCE_DM_ON_OK", "false"), False)
AI_MAINTENANCE_DM_ON_WARNING = parse_bool(os.getenv("AI_MAINTENANCE_DM_ON_WARNING", "true"), True)
AI_MAINTENANCE_DM_ON_CRITICAL = parse_bool(os.getenv("AI_MAINTENANCE_DM_ON_CRITICAL", "true"), True)
AI_MAINTENANCE_COOLDOWN_SECONDS = int(os.getenv("AI_MAINTENANCE_COOLDOWN_SECONDS", "900"))
AI_MAINTENANCE_START_DELAY_SECONDS = float(os.getenv("AI_MAINTENANCE_START_DELAY_SECONDS", "20"))
AI_CHAT_ENABLED = parse_bool(os.getenv("AI_CHAT_ENABLED", os.getenv("AI_MAINTENANCE_ENABLED", "false")), False)
AI_CHAT_ALLOWED_IDS = parse_id_list(os.getenv("AI_CHAT_ALLOWED_IDS", "")) or AI_MAINTENANCE_ALERT_IDS
AI_CHAT_PROVIDER = os.getenv("AI_CHAT_PROVIDER", AI_PROVIDER).strip().lower() or AI_PROVIDER
AI_CHAT_MODEL = os.getenv("AI_CHAT_MODEL", AI_MODEL_CHAT).strip() or AI_MODEL_CHAT
AI_CHAT_MAX_HISTORY = int(os.getenv("AI_CHAT_MAX_HISTORY", "12"))
AI_CHAT_MAX_REPLY_CHARS = int(os.getenv("AI_CHAT_MAX_REPLY_CHARS", "1800"))
AI_CHAT_COOLDOWN_SECONDS = float(os.getenv("AI_CHAT_COOLDOWN_SECONDS", "3"))
AI_CHAT_MAX_MESSAGE_CHARS = int(os.getenv("AI_CHAT_MAX_MESSAGE_CHARS", "1800"))
AI_CHAT_R2_STATS_ENABLED = parse_bool(os.getenv("AI_CHAT_R2_STATS_ENABLED", "true"), True)
AI_CHAT_R2_STATS_CACHE_SECONDS = int(os.getenv("AI_CHAT_R2_STATS_CACHE_SECONDS", "900"))
AI_CHAT_R2_STATS_MAX_PAGES = int(os.getenv("AI_CHAT_R2_STATS_MAX_PAGES", "2000"))
AI_CHAT_SERVER_REPLIES_ENABLED = parse_bool(os.getenv("AI_CHAT_SERVER_REPLIES_ENABLED", "true"), True)
AI_CHAT_SERVER_REQUIRE_MENTION = parse_bool(os.getenv("AI_CHAT_SERVER_REQUIRE_MENTION", "true"), True)
AI_CHAT_SERVER_KNOWLEDGE_ENABLED = parse_bool(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_ENABLED", "true"), True)
AI_CHAT_SERVER_KNOWLEDGE_CACHE_SECONDS = int(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_CACHE_SECONDS", "600"))
AI_CHAT_SERVER_KNOWLEDGE_MAX_MESSAGES = int(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_MAX_MESSAGES", "12"))
AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS = int(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS", "9000"))
AI_CHAT_SERVER_KNOWLEDGE_CHANNEL_NAMES = {
    item.lower()
    for item in parse_str_list(
        os.getenv(
            "AI_CHAT_SERVER_KNOWLEDGE_CHANNEL_NAMES",
            "rules,rule,server-rules,resources,resource,panduan,guide,guides,announcement,announcements,pengumuman,link-invite,welcome",
        )
    )
}
AI_ATTACHMENT_ENABLED = parse_bool(os.getenv("AI_ATTACHMENT_ENABLED", "true"), True)
AI_ATTACHMENT_OCR_ENABLED = parse_bool(os.getenv("AI_ATTACHMENT_OCR_ENABLED", "true"), True)
AI_ATTACHMENT_MAX_BYTES = int(os.getenv("AI_ATTACHMENT_MAX_BYTES", str(8 * 1024 * 1024)))
AI_ATTACHMENT_MAX_TEXT_CHARS = int(os.getenv("AI_ATTACHMENT_MAX_TEXT_CHARS", "12000"))
AI_ATTACHMENT_CACHE_SECONDS = int(os.getenv("AI_ATTACHMENT_CACHE_SECONDS", "900"))
AI_ATTACHMENT_VISION_PROVIDER = os.getenv(
    "AI_ATTACHMENT_VISION_PROVIDER",
    "gemini" if GEMINI_API_KEY else AI_CHAT_PROVIDER,
).strip().lower() or ("gemini" if GEMINI_API_KEY else AI_CHAT_PROVIDER)
AI_ATTACHMENT_VISION_MODEL = os.getenv(
    "AI_ATTACHMENT_VISION_MODEL",
    "gemini-2.5-flash-lite" if AI_ATTACHMENT_VISION_PROVIDER == "gemini" else AI_CHAT_MODEL,
).strip() or ("gemini-2.5-flash-lite" if AI_ATTACHMENT_VISION_PROVIDER == "gemini" else AI_CHAT_MODEL)
AI_OPERATOR_ENABLED = parse_bool(os.getenv("AI_OPERATOR_ENABLED", os.getenv("AI_MAINTENANCE_ENABLED", "false")), False)
AI_OPERATOR_ALLOWED_IDS = parse_id_list(os.getenv("AI_OPERATOR_ALLOWED_IDS", "")) or AI_MAINTENANCE_ALERT_IDS
AI_OPERATOR_REQUIRE_CONFIRMATION = parse_bool(os.getenv("AI_OPERATOR_REQUIRE_CONFIRMATION", "true"), True)
AI_OPERATOR_APPROVAL_TTL_SECONDS = int(os.getenv("AI_OPERATOR_APPROVAL_TTL_SECONDS", "900"))
AI_OPERATOR_PROPOSAL_COOLDOWN_SECONDS = int(os.getenv("AI_OPERATOR_PROPOSAL_COOLDOWN_SECONDS", "1800"))
AI_OPERATOR_MAX_PENDING = int(os.getenv("AI_OPERATOR_MAX_PENDING", "10"))
AI_OPERATOR_ALLOW_R2_MAINTENANCE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_R2_MAINTENANCE", "true"), True)
AI_OPERATOR_ALLOW_STEAM_DB_SYNC = parse_bool(os.getenv("AI_OPERATOR_ALLOW_STEAM_DB_SYNC", "true"), True)
AI_OPERATOR_ALLOW_AI_RECHECK = parse_bool(os.getenv("AI_OPERATOR_ALLOW_AI_RECHECK", "true"), True)
AI_OPERATOR_ALLOW_SERVER_AUDIT = parse_bool(os.getenv("AI_OPERATOR_ALLOW_SERVER_AUDIT", "true"), True)
AI_OPERATOR_ALLOW_BOOSTER_SYNC = parse_bool(os.getenv("AI_OPERATOR_ALLOW_BOOSTER_SYNC", "true"), True)
AI_OPERATOR_ALLOW_SEND_ANNOUNCEMENT = parse_bool(os.getenv("AI_OPERATOR_ALLOW_SEND_ANNOUNCEMENT", "true"), True)
AI_OPERATOR_ALLOW_UPDATE_RULES = parse_bool(os.getenv("AI_OPERATOR_ALLOW_UPDATE_RULES", "true"), True)
AI_OPERATOR_ALLOW_PIN_MESSAGE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_PIN_MESSAGE", "true"), True)
AI_OPERATOR_ALLOW_SET_CHANNEL_TOPIC = parse_bool(os.getenv("AI_OPERATOR_ALLOW_SET_CHANNEL_TOPIC", "true"), True)
AI_OPERATOR_ALLOW_CREATE_CHANNEL = parse_bool(os.getenv("AI_OPERATOR_ALLOW_CREATE_CHANNEL", "true"), True)
AI_OPERATOR_ALLOW_CONFIGURE_CHANNEL_ACCESS = parse_bool(
    os.getenv("AI_OPERATOR_ALLOW_CONFIGURE_CHANNEL_ACCESS", "true"),
    True,
)

SERVER_ADMIN_ENABLED = parse_bool(os.getenv("SERVER_ADMIN_ENABLED", "false"), False)
SERVER_ADMIN_GUILD_IDS = parse_id_set(os.getenv("SERVER_ADMIN_GUILD_IDS", ""))
SERVER_ADMIN_AUDIT_ON_START = parse_bool(os.getenv("SERVER_ADMIN_AUDIT_ON_START", "true"), True)
SERVER_ADMIN_AUDIT_INTERVAL_HOURS = float(os.getenv("SERVER_ADMIN_AUDIT_INTERVAL_HOURS", "6"))
SERVER_ADMIN_START_DELAY_SECONDS = float(os.getenv("SERVER_ADMIN_START_DELAY_SECONDS", "30"))
SERVER_ADMIN_ALERT_ON_ISSUES = parse_bool(os.getenv("SERVER_ADMIN_ALERT_ON_ISSUES", "true"), True)
SERVER_ADMIN_RULES_CHANNEL_NAMES = {
    item.lower()
    for item in parse_str_list(os.getenv("SERVER_ADMIN_RULES_CHANNEL_NAMES", "rules,rule,server-rules"))
}
SERVER_ADMIN_ANNOUNCEMENT_CHANNEL_NAMES = {
    item.lower()
    for item in parse_str_list(
        os.getenv("SERVER_ADMIN_ANNOUNCEMENT_CHANNEL_NAMES", "announcement,announcements,pengumuman")
    )
}
SERVER_ADMIN_REQUIRED_PERMISSIONS = {
    item.strip().lower()
    for item in parse_str_list(
        os.getenv(
            "SERVER_ADMIN_REQUIRED_PERMISSIONS",
            "administrator,manage_roles,manage_channels,send_messages,embed_links,read_message_history,manage_messages,moderate_members",
        )
    )
}
SECURITY_BOT_AUDIT_ENABLED = parse_bool(os.getenv("SECURITY_BOT_AUDIT_ENABLED", "true"), True)
SECURITY_BOT_IDS = parse_id_set(os.getenv("SECURITY_BOT_IDS", "651095740390834176"))
SECURITY_BOT_NAMES = {
    item.lower()
    for item in parse_str_list(os.getenv("SECURITY_BOT_NAMES", "security,security bot"))
}
SECURITY_BOT_ROLE_IDS = parse_id_set(os.getenv("SECURITY_BOT_ROLE_IDS", ""))
SECURITY_BOT_ROLE_NAMES = {
    item.lower()
    for item in parse_str_list(os.getenv("SECURITY_BOT_ROLE_NAMES", "security"))
}
SECURITY_BOT_REQUIRED_PERMISSIONS = {
    item.strip().lower()
    for item in parse_str_list(os.getenv("SECURITY_BOT_REQUIRED_PERMISSIONS", "administrator"))
}
SECURITY_BOT_LOG_CHANNEL_REQUIRED = parse_bool(os.getenv("SECURITY_BOT_LOG_CHANNEL_REQUIRED", "false"), False)
SECURITY_BOT_LOG_CHANNEL_NAMES = {
    item.lower()
    for item in parse_str_list(
        os.getenv("SECURITY_BOT_LOG_CHANNEL_NAMES", "security-logs,security-log,mod-logs,audit-log,logs")
    )
}

STEAM_API_KEY    = os.getenv("STEAM_API_KEY", "")
STEAM_STORE_API  = "https://store.steampowered.com/api/appdetails"
STEAM_SEARCH_API = "https://store.steampowered.com/api/storesearch"

GITHUB_API_BASE      = "https://api.github.com/repos/SteamAutoCracks/ManifestHub"
GITHUB_BRANCHES_URL  = f"{GITHUB_API_BASE}/branches"
MANIFESTHUB_PATH     = os.getenv("MANIFESTHUB_PATH", "SteamAutoCracks/ManifestHub")

DB_PATH         = env_path("DB_PATH", DATA_DIR / "games.json")
GEN_USAGE_PATH  = env_path("GEN_USAGE_PATH", DATA_DIR / "gen_usage.json")
BACKFILL_STATE  = DATA_DIR / "backfill_state.json"
CRAWLER_STATE   = DATA_DIR / "crawler_state.json"
CACHE_FILE      = DATA_DIR / "cache.json"

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE        = LOGS_DIR / "bot.log"
LOG_FORMAT      = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

BOT_PREFIX      = os.getenv("BOT_PREFIX", "!")
BOT_VERSION     = "9.2.7"
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
