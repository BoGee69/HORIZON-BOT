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


BASE_DIR = Path(__file__).parent
LOCAL_DATA_DIR = BASE_DIR / "data"


def _running_on_railway() -> bool:
    return any(
        os.getenv(name)
        for name in (
            "RAILWAY_ENVIRONMENT",
            "RAILWAY_PROJECT_ID",
            "RAILWAY_SERVICE_ID",
            "RAILWAY_DEPLOYMENT_ID",
        )
    )


def _default_data_dir() -> Path:
    # Railway volume mount from the Settings page is /data in this project.
    # Prefer the explicit Railway env var when present, otherwise auto-use /data
    # when the service is running on Railway and the mount exists.
    railway_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if railway_mount:
        return Path(railway_mount)

    if _running_on_railway() and Path("/data").exists():
        return Path("/data")

    return LOCAL_DATA_DIR


def env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip().strip('"').strip("'")
    if raw and os.name == "nt" and not _running_on_railway() and (raw == "/data" or raw.startswith("/data/")):
        suffix = raw.removeprefix("/data").lstrip("/\\")
        path = LOCAL_DATA_DIR / suffix if suffix else LOCAL_DATA_DIR
    else:
        path = Path(raw) if raw else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


DATA_DIR = env_path("DATA_DIR", _default_data_dir())
LOGS_DIR = env_path("LOGS_DIR", BASE_DIR / "logs")
CACHE_DIR = env_path("CACHE_DIR", DATA_DIR / "cache_games")
FILES_DIR = env_path("FILES_DIR", BASE_DIR / "Files")

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

# Public /request routing. If REQUEST_CHANNEL_IDS is set, requests are posted
# there. Otherwise TriadBot looks for these channel names, then falls back to
# the channel where the command was used.
REQUEST_CHANNEL_IDS = parse_id_set(os.getenv("REQUEST_CHANNEL_IDS", ""))
REQUEST_CHANNEL_NAMES = {
    x.strip().lower()
    for x in os.getenv(
        "REQUEST_CHANNEL_NAMES",
        "request,requests,game-request,game-requests,request-game,request-games",
    ).split(",")
    if x.strip()
}
REQUEST_PING_ADMINS = parse_bool(os.getenv("REQUEST_PING_ADMINS", "true"), True)
GAME_REQUESTS_PATH = env_path("GAME_REQUESTS_PATH", DATA_DIR / "game_requests.json")

R2_BASE_URL = os.getenv("R2_BASE_URL", "")

# FIX 3: WEB_URL — WAJIB di-set di your .env file env agar JWT link tidak nunjuk ke localhost.
# Isi dengan URL your .env file kamu, e.g. https://triadbot-production.up.railway.app
WEB_URL    = os.getenv("WEB_URL", DEFAULT_WEB_URL).rstrip("/")
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
PORT       = int(os.getenv("PORT", "8080"))

R2_ACCESS_KEY_ID     = os.getenv("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
R2_ACCOUNT_ID        = os.getenv("R2_ACCOUNT_ID", "")
R2_BUCKET_NAME       = os.getenv("R2_BUCKET_NAME", "")
LINK_EXPIRE_SECONDS  = int(os.getenv("LINK_EXPIRE_SECONDS", "3600"))

# Open Directory -> R2 sync (disabled by default). Use only for sources you own or have permission to mirror.
OPENDIR_SYNC_ENABLED = parse_bool(os.getenv("OPENDIR_SYNC_ENABLED", "false"), False)
OPENDIR_BASE_URL = os.getenv("OPENDIR_BASE_URL", os.getenv("OPEN_DIRECTORY_URL", "")).strip()
OPENDIR_R2_PREFIX = os.getenv("OPENDIR_R2_PREFIX", "Database/").strip()
OPENDIR_SOURCE_MODE = os.getenv("OPENDIR_SOURCE_MODE", "api").strip().lower() or "api"
OPENDIR_API_BASE_URL = os.getenv("OPENDIR_API_BASE_URL", OPENDIR_BASE_URL).strip()
OPENDIR_API_SEARCH_PATH = os.getenv("OPENDIR_API_SEARCH_PATH", "/api/search").strip() or "/api/search"
OPENDIR_API_GENERATE_PATH = os.getenv("OPENDIR_API_GENERATE_PATH", "/api/generate").strip() or "/api/generate"
OPENDIR_API_DEFAULT_MANIFEST_ID = os.getenv("OPENDIR_API_DEFAULT_MANIFEST_ID", "7884779798207988041").strip()
OPENDIR_API_BRANCH = os.getenv("OPENDIR_API_BRANCH", "public").strip() or "public"
OPENDIR_API_DEPOT_KEY = os.getenv("OPENDIR_API_DEPOT_KEY", "").strip()
OPENDIR_API_USE_RYUU_API = parse_bool(os.getenv("OPENDIR_API_USE_RYUU_API", "true"), True)
OPENDIR_API_LOOKUP_BEFORE_GENERATE = parse_bool(os.getenv("OPENDIR_API_LOOKUP_BEFORE_GENERATE", "true"), True)
OPENDIR_API_CLEAN_BEFORE_UPLOAD = parse_bool(os.getenv("OPENDIR_API_CLEAN_BEFORE_UPLOAD", "true"), True)
OPENDIR_API_BUFFER_MAX_MB = int(os.getenv("OPENDIR_API_BUFFER_MAX_MB", os.getenv("R2_MAINTENANCE_MAX_ZIP_MB", "50")))
OPENDIR_API_GENERATE_RETRIES = int(os.getenv("OPENDIR_API_GENERATE_RETRIES", "3"))
OPENDIR_API_RETRY_DELAY_SECONDS = float(os.getenv("OPENDIR_API_RETRY_DELAY_SECONDS", "2"))
OPENDIR_API_REQUEST_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_API_REQUEST_TIMEOUT_SECONDS", os.getenv("OPENDIR_REQUEST_TIMEOUT_SECONDS", "900")))
OPENDIR_API_CONNECT_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_API_CONNECT_TIMEOUT_SECONDS", os.getenv("OPENDIR_CONNECT_TIMEOUT_SECONDS", "30")))
OPENDIR_API_READ_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_API_READ_TIMEOUT_SECONDS", "300"))
OPENDIR_API_PENDING_RETRY_ENABLED = parse_bool(os.getenv("OPENDIR_API_PENDING_RETRY_ENABLED", "true"), True)
OPENDIR_API_PENDING_RETRY_DELAY_SECONDS = float(os.getenv("OPENDIR_API_PENDING_RETRY_DELAY_SECONDS", "180"))
OPENDIR_API_PENDING_RETRY_MAX_ATTEMPTS = int(os.getenv("OPENDIR_API_PENDING_RETRY_MAX_ATTEMPTS", "12"))
OPENDIR_API_PENDING_RETRY_MAX_PER_RUN = int(os.getenv("OPENDIR_API_PENDING_RETRY_MAX_PER_RUN", "50"))
OPENDIR_API_PENDING_RETRY_MAX_QUEUE = int(os.getenv("OPENDIR_API_PENDING_RETRY_MAX_QUEUE", "5000"))
OPENDIR_PRIORITY_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_PRIORITY_TIMEOUT_SECONDS", "900"))
OPENDIR_PRIORITY_API_GENERATE_RETRIES = int(os.getenv("OPENDIR_PRIORITY_API_GENERATE_RETRIES", "2"))
OPENDIR_PRIORITY_API_REQUEST_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_PRIORITY_API_REQUEST_TIMEOUT_SECONDS", "180"))
OPENDIR_PRIORITY_API_READ_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_PRIORITY_API_READ_TIMEOUT_SECONDS", "90"))
OPENDIR_PRIORITY_PENDING_RETRIES = int(os.getenv("OPENDIR_PRIORITY_PENDING_RETRIES", "8"))
OPENDIR_PRIORITY_PENDING_DELAY_SECONDS = float(os.getenv("OPENDIR_PRIORITY_PENDING_DELAY_SECONDS", "45"))
OPENDIR_INTERVAL_HOURS = float(os.getenv("OPENDIR_INTERVAL_HOURS", os.getenv("SYNC_INTERVAL_HOURS", "6")))
OPENDIR_RUN_ON_START = parse_bool(os.getenv("OPENDIR_RUN_ON_START", "true"), True)
OPENDIR_START_DELAY_SECONDS = float(os.getenv("OPENDIR_START_DELAY_SECONDS", "20"))
OPENDIR_MAX_DEPTH = int(os.getenv("OPENDIR_MAX_DEPTH", "3"))
OPENDIR_MAX_FILES_PER_RUN = int(os.getenv("OPENDIR_MAX_FILES_PER_RUN", "0"))  # 0 = no explicit per-run cap
OPENDIR_MAX_FILE_MB = int(os.getenv("OPENDIR_MAX_FILE_MB", "1024"))
OPENDIR_CONCURRENCY = int(os.getenv("OPENDIR_CONCURRENCY", "2"))
OPENDIR_QUEUE_CHUNKS = int(os.getenv("OPENDIR_QUEUE_CHUNKS", "8"))
OPENDIR_CHUNK_SIZE_BYTES = int(os.getenv("OPENDIR_CHUNK_SIZE_BYTES", str(1024 * 1024)))
OPENDIR_REQUEST_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_REQUEST_TIMEOUT_SECONDS", "900"))
OPENDIR_CONNECT_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_CONNECT_TIMEOUT_SECONDS", "30"))
OPENDIR_READ_TIMEOUT_SECONDS = float(os.getenv("OPENDIR_READ_TIMEOUT_SECONDS", "120"))
OPENDIR_USE_HEAD = parse_bool(os.getenv("OPENDIR_USE_HEAD", "true"), True)
OPENDIR_NOTIFY_ON_SUCCESS = parse_bool(os.getenv("OPENDIR_NOTIFY_ON_SUCCESS", "false"), False)
OPENDIR_FLATTEN_R2_KEYS = parse_bool(os.getenv("OPENDIR_FLATTEN_R2_KEYS", "false"), False)
OPENDIR_ALLOWED_EXTENSIONS = parse_csv_set(os.getenv("OPENDIR_ALLOWED_EXTENSIONS", "zip,manifest,lua,acf,vdf"))
OPENDIR_ALLOWED_HOSTS = {x.strip().lower() for x in os.getenv("OPENDIR_ALLOWED_HOSTS", "").split(",") if x.strip()}
OPENDIR_USER_AGENT = os.getenv("OPENDIR_USER_AGENT", "TriadBot OpenDirSync").strip()

OPENDIR_STATE_PATH = env_path("OPENDIR_STATE_PATH", DATA_DIR / "opendir_sync_state.json")
OPENDIR_INDEX_SCAN_ENABLED = parse_bool(os.getenv("OPENDIR_INDEX_SCAN_ENABLED", "true"), True)
OPENDIR_DIRECT_PROBE_ENABLED = parse_bool(os.getenv("OPENDIR_DIRECT_PROBE_ENABLED", "true"), True)
OPENDIR_FALLBACK_GET_PROBE = parse_bool(os.getenv("OPENDIR_FALLBACK_GET_PROBE", "true"), True)
OPENDIR_GAMES_PER_RUN     = int(os.getenv("OPENDIR_GAMES_PER_RUN", "500"))       # backward compat
OPENDIR_MAX_GAMES_PER_RUN = int(os.getenv("OPENDIR_MAX_GAMES_PER_RUN",            # name used by cog
                                           os.getenv("OPENDIR_GAMES_PER_RUN", "500")))
OPENDIR_TARGET_EXTENSIONS = parse_csv_set(os.getenv("OPENDIR_TARGET_EXTENSIONS", "zip"), "zip")
OPENDIR_SOURCE_PATTERNS = parse_str_list(os.getenv(
    "OPENDIR_SOURCE_PATTERNS",
    "{appid}.{ext},{appid}/{appid}.{ext},{target_filename},{safe_name}.{ext},{safe_name} ({appid}).{ext}",
))
# How long (seconds) to wait for a SQLite write lock before giving up.
OPENDIR_SQLITE_WAIT_TIMEOUT_SECONDS = int(os.getenv("OPENDIR_SQLITE_WAIT_TIMEOUT_SECONDS", "600"))
# How old (hours) the R2 SQLite inventory cache may be before a full rebuild is triggered.
OPENDIR_R2_CACHE_TTL_HOURS = float(os.getenv("OPENDIR_R2_CACHE_TTL_HOURS", "12"))

# Backwards compatibility for the earlier cogs/opendir_sync.py variable names.
OPEN_DIRECTORY_URL = OPENDIR_BASE_URL
SYNC_INTERVAL_HOURS = OPENDIR_INTERVAL_HOURS

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

STEAM_DB_SYNC_ENABLED = parse_bool(os.getenv("STEAM_DB_SYNC_ENABLED", "true"), True)
STEAM_DB_SYNC_APPLY = parse_bool(os.getenv("STEAM_DB_SYNC_APPLY", "true"), True)
STEAM_DB_SYNC_RUN_ON_START = parse_bool(os.getenv("STEAM_DB_SYNC_RUN_ON_START", "true"), True)
STEAM_DB_SYNC_START_DELAY_SECONDS = float(os.getenv("STEAM_DB_SYNC_START_DELAY_SECONDS", "5"))
STEAM_DB_SYNC_INTERVAL_HOURS = float(os.getenv("STEAM_DB_SYNC_INTERVAL_HOURS", "6"))
if STEAM_DB_SYNC_INTERVAL_HOURS > 6:
    STEAM_DB_SYNC_INTERVAL_HOURS = 6.0
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
AI_MAINTENANCE_MODEL = os.getenv(
    "AI_MAINTENANCE_MODEL",
    "gpt-oss:120b" if AI_MAINTENANCE_PROVIDER == "ollama" else "gemini-2.5-flash-lite",
).strip() or ("gpt-oss:120b" if AI_MAINTENANCE_PROVIDER == "ollama" else "gemini-2.5-flash-lite")
AI_MAINTENANCE_INTERVAL_MINUTES = float(os.getenv("AI_MAINTENANCE_INTERVAL_MINUTES", "360"))
AI_MAINTENANCE_ALERT_IDS = parse_id_list(os.getenv("AI_MAINTENANCE_ALERT_IDS", "")) or ADMIN_IDS[:1]
AI_MAINTENANCE_MAX_PROMPT_CHARS = int(os.getenv("AI_MAINTENANCE_MAX_PROMPT_CHARS", "12000"))
AI_MAINTENANCE_DM_ON_OK = parse_bool(os.getenv("AI_MAINTENANCE_DM_ON_OK", "false"), False)
AI_MAINTENANCE_DM_ON_WARNING = parse_bool(os.getenv("AI_MAINTENANCE_DM_ON_WARNING", "true"), True)
AI_MAINTENANCE_DM_ON_CRITICAL = parse_bool(os.getenv("AI_MAINTENANCE_DM_ON_CRITICAL", "true"), True)
AI_MAINTENANCE_COOLDOWN_SECONDS = int(os.getenv("AI_MAINTENANCE_COOLDOWN_SECONDS", "900"))
AI_MAINTENANCE_START_DELAY_SECONDS = float(os.getenv("AI_MAINTENANCE_START_DELAY_SECONDS", "20"))
AI_CHAT_ENABLED = parse_bool(os.getenv("AI_CHAT_ENABLED", os.getenv("AI_MAINTENANCE_ENABLED", "true")), True)
AI_CHAT_ALLOWED_IDS = parse_id_list(os.getenv("AI_CHAT_ALLOWED_IDS", "")) or AI_MAINTENANCE_ALERT_IDS
AI_CHAT_ALLOW_DISCORD_ADMINS = parse_bool(os.getenv("AI_CHAT_ALLOW_DISCORD_ADMINS", "true"), True)
AI_CHAT_PROVIDER = os.getenv("AI_CHAT_PROVIDER", AI_PROVIDER).strip().lower() or AI_PROVIDER
AI_CHAT_MODEL = os.getenv(
    "AI_CHAT_MODEL",
    "gpt-oss:120b" if AI_CHAT_PROVIDER == "ollama" else AI_MAINTENANCE_MODEL,
).strip() or ("gpt-oss:120b" if AI_CHAT_PROVIDER == "ollama" else AI_MAINTENANCE_MODEL)
AI_CHAT_MAX_HISTORY = int(os.getenv("AI_CHAT_MAX_HISTORY", "12"))
AI_CHAT_MAX_REPLY_CHARS = int(os.getenv("AI_CHAT_MAX_REPLY_CHARS", "1800"))
AI_CHAT_RESPONSE_TIMEOUT_SECONDS = float(os.getenv("AI_CHAT_RESPONSE_TIMEOUT_SECONDS", "60"))
AI_CHAT_COOLDOWN_SECONDS = float(os.getenv("AI_CHAT_COOLDOWN_SECONDS", "3"))
AI_CHAT_MAX_MESSAGE_CHARS = int(os.getenv("AI_CHAT_MAX_MESSAGE_CHARS", "1800"))
AI_CHAT_R2_STATS_ENABLED = parse_bool(os.getenv("AI_CHAT_R2_STATS_ENABLED", "true"), True)
AI_CHAT_R2_STATS_CACHE_SECONDS = int(os.getenv("AI_CHAT_R2_STATS_CACHE_SECONDS", "900"))
AI_CHAT_R2_STATS_MAX_PAGES = int(os.getenv("AI_CHAT_R2_STATS_MAX_PAGES", "2000"))
AI_CHAT_R2_STATS_TIMEOUT_SECONDS = float(os.getenv("AI_CHAT_R2_STATS_TIMEOUT_SECONDS", "8"))
AI_CHAT_SERVER_REPLIES_ENABLED = parse_bool(os.getenv("AI_CHAT_SERVER_REPLIES_ENABLED", "true"), True)
AI_CHAT_SERVER_REQUIRE_MENTION = parse_bool(os.getenv("AI_CHAT_SERVER_REQUIRE_MENTION", "true"), True)
AI_CHAT_PUBLIC_INFO_ONLY = parse_bool(os.getenv("AI_CHAT_PUBLIC_INFO_ONLY", "true"), True)
AI_CHAT_SERVER_KNOWLEDGE_ENABLED = parse_bool(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_ENABLED", "true"), True)
AI_CHAT_SERVER_KNOWLEDGE_CACHE_SECONDS = int(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_CACHE_SECONDS", "600"))
AI_CHAT_SERVER_KNOWLEDGE_MAX_MESSAGES = int(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_MAX_MESSAGES", "12"))
AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS = int(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_MAX_CHARS", "9000"))
AI_CHAT_SERVER_KNOWLEDGE_TIMEOUT_SECONDS = float(os.getenv("AI_CHAT_SERVER_KNOWLEDGE_TIMEOUT_SECONDS", "8"))
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
AI_OPERATOR_ENABLED = parse_bool(os.getenv("AI_OPERATOR_ENABLED", os.getenv("AI_MAINTENANCE_ENABLED", "true")), True)
AI_OPERATOR_ALLOWED_IDS = parse_id_list(os.getenv("AI_OPERATOR_ALLOWED_IDS", "")) or AI_MAINTENANCE_ALERT_IDS
AI_OPERATOR_ALLOW_DISCORD_ADMINS = parse_bool(os.getenv("AI_OPERATOR_ALLOW_DISCORD_ADMINS", "true"), True)
AI_OPERATOR_DM_ONLY = parse_bool(os.getenv("AI_OPERATOR_DM_ONLY", "true"), True)
AI_OPERATOR_SERVER_PROMPTS_ENABLED = parse_bool(os.getenv("AI_OPERATOR_SERVER_PROMPTS_ENABLED", "false"), False)
AI_OPERATOR_SERVER_REQUIRE_MENTION = parse_bool(os.getenv("AI_OPERATOR_SERVER_REQUIRE_MENTION", "true"), True)
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
AI_OPERATOR_ALLOW_SETUP_CHANNEL_TEMPLATE = parse_bool(
    os.getenv("AI_OPERATOR_ALLOW_SETUP_CHANNEL_TEMPLATE", "true"),
    True,
)
AI_OPERATOR_ALLOW_CREATE_ROLE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_CREATE_ROLE", "true"), True)
AI_OPERATOR_ALLOW_UPDATE_ROLE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_UPDATE_ROLE", "true"), True)
AI_OPERATOR_ALLOW_DELETE_ROLE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_DELETE_ROLE", "false"), False)
AI_OPERATOR_ALLOW_MEMBER_TIMEOUT = parse_bool(os.getenv("AI_OPERATOR_ALLOW_MEMBER_TIMEOUT", "true"), True)
AI_OPERATOR_ALLOW_MEMBER_KICK = parse_bool(os.getenv("AI_OPERATOR_ALLOW_MEMBER_KICK", "false"), False)
AI_OPERATOR_ALLOW_MEMBER_BAN = parse_bool(os.getenv("AI_OPERATOR_ALLOW_MEMBER_BAN", "false"), False)
AI_OPERATOR_ALLOW_WEBHOOK_CREATE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_WEBHOOK_CREATE", "true"), True)
AI_OPERATOR_ALLOW_WEBHOOK_DELETE = parse_bool(os.getenv("AI_OPERATOR_ALLOW_WEBHOOK_DELETE", "false"), False)
AI_OPERATOR_ALLOW_SERVER_SETTING = parse_bool(os.getenv("AI_OPERATOR_ALLOW_SERVER_SETTING", "false"), False)
AI_OPERATOR_ALLOW_SCHEDULE_ACTION = parse_bool(os.getenv("AI_OPERATOR_ALLOW_SCHEDULE_ACTION", "true"), True)
AI_OPERATOR_SCHEDULES_PATH = env_path("AI_OPERATOR_SCHEDULES_PATH", DATA_DIR / "ai_operator_schedules.json")
AI_OPERATOR_SCHEDULER_ENABLED = parse_bool(os.getenv("AI_OPERATOR_SCHEDULER_ENABLED", "true"), True)
AI_OPERATOR_SCHEDULE_CHECK_SECONDS = int(os.getenv("AI_OPERATOR_SCHEDULE_CHECK_SECONDS", "60"))

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

SQLITE_PATH = env_path("SQLITE_PATH", DATA_DIR / "games.db")

# Railway SQLite -> GitHub private repo backup.
# Use a fine-grained GitHub token with repository Contents: Read and write.
GITHUB_BACKUP_ENABLED = parse_bool(os.getenv("GITHUB_BACKUP_ENABLED", "true"), True)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip().strip("/")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip() or "main"
GITHUB_DB_PATH = os.getenv("GITHUB_DB_PATH", "games.db").strip().strip("/") or "games.db"
GITHUB_DB_METADATA_PATH = os.getenv(
    "GITHUB_DB_METADATA_PATH",
    f"{GITHUB_DB_PATH}.meta.json",
).strip().strip("/")
GITHUB_BACKUP_INTERVAL_HOURS = float(os.getenv("GITHUB_BACKUP_INTERVAL_HOURS", "12"))
GITHUB_BACKUP_START_DELAY_SECONDS = float(os.getenv("GITHUB_BACKUP_START_DELAY_SECONDS", "90"))
GITHUB_BACKUP_NOTIFY_ON_SUCCESS = parse_bool(os.getenv("GITHUB_BACKUP_NOTIFY_ON_SUCCESS", "false"), False)
GITHUB_BACKUP_TIMEOUT_SECONDS = int(os.getenv("GITHUB_BACKUP_TIMEOUT_SECONDS", "180"))
GITHUB_BACKUP_CHUNK_SIZE_MB = int(os.getenv("GITHUB_BACKUP_CHUNK_SIZE_MB", "8"))

GEN_USAGE_PATH = env_path("GEN_USAGE_PATH", DATA_DIR / "gen_usage.json")
BACKFILL_STATE = env_path("BACKFILL_STATE", DATA_DIR / "backfill_state.json")
CRAWLER_STATE = env_path("CRAWLER_STATE", DATA_DIR / "crawler_state.json")
CACHE_FILE = env_path("CACHE_FILE", DATA_DIR / "cache.json")

LOG_LEVEL       = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE        = LOGS_DIR / "bot.log"
LOG_FORMAT      = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

BOT_PREFIX      = os.getenv("BOT_PREFIX", "!")
BOT_VERSION     = "9.2.24"
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
            "Set WEB_URL=https://<nama-app>.up.railway.app di your .env file environment variables."
        )
    if errors:
        raise ValueError(f"Configuration errors: {', '.join(errors)}")


validate_config()
