"""
Helper utilities for bot operations
"""
import re
import logging
from typing import Optional

log = logging.getLogger(__name__)


def clean_search_string(text: Optional[str]) -> str:
    """Remove non-alphanumeric characters for search matching"""
    if not text:
        return ""
    return "".join(c for c in str(text).lower() if c.isalnum())


def make_safe_filename(name: str, max_length: int = 200) -> str:
    """
    Create safe filename from game name
    Removes invalid characters and limits length
    """
    if not name:
        return "Unknown"
    
    # Keep only alphanumeric, spaces, hyphens, underscores
    safe = "".join(
        c for c in name
        if c.isalnum() or c in (' ', '-', '_')
    ).strip()
    
    # Limit length
    if len(safe) > max_length:
        safe = safe[:max_length].strip()
    
    return safe or "Unknown"


def extract_appid_from_filename(filename: str) -> Optional[str]:
    """Extract AppID from filename like 'GameName [12345].zip'"""
    pattern = re.compile(r'\[(\d+)\]')
    match = pattern.search(filename)
    return match.group(1) if match else None


def format_size(bytes_size: int) -> str:
    """Format bytes into human-readable size"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_size < 1024.0:
            return f"{bytes_size:.2f} {unit}"
        bytes_size /= 1024.0
    return f"{bytes_size:.2f} PB"


def format_number(num: int) -> str:
    """Format number with thousand separators"""
    return f"{num:,}"


def truncate_text(text: str, max_length: int = 300, suffix: str = "...") -> str:
    """Truncate text to max length with suffix"""
    if not text or len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)].strip() + suffix


def is_valid_appid(appid: str) -> bool:
    """Check if AppID is valid (numeric and reasonable range)"""
    if not appid or not appid.isdigit():
        return False
    
    appid_int = int(appid)
    return 0 < appid_int < 10000000  # Steam AppIDs are typically in this range


def has_any_role_name(user, role_names) -> bool:
    """Check Discord member roles by name, case-insensitive."""
    if not role_names:
        return False
    roles = getattr(user, "roles", []) or []
    member_roles = {getattr(role, "name", "").lower() for role in roles}
    return bool(member_roles.intersection(role_names))


def is_admin_interaction(interaction, admin_ids, admin_role_names) -> bool:
    """Allow configured IDs, server owner, Administrator permission, or configured admin roles."""
    user = interaction.user
    if user.id in admin_ids:
        return True

    guild = interaction.guild
    if guild and guild.owner_id == user.id:
        return True

    permissions = getattr(user, "guild_permissions", None)
    if permissions and getattr(permissions, "administrator", False):
        return True

    return has_any_role_name(user, admin_role_names)


def parse_github_url(url: str) -> str:
    """Convert GitHub URL to raw content URL"""
    if "github.com" in url and "raw" not in url:
        return url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    return url


def is_valid_github_url(url: str) -> bool:
    """Check if URL is a valid GitHub URL"""
    if not url.startswith("https://"):
        return False
    
    return "github.com" in url or "githubusercontent.com" in url


def extract_protection_type(drm_notice: Optional[str]) -> str:
    """Extract DRM protection type from Steam data"""
    if not drm_notice:
        return "Unknown"
    
    drm_lower = drm_notice.lower()
    
    if "denuvo" in drm_lower:
        return "Denuvo"
    elif "steam drm" in drm_lower or "steam" in drm_lower:
        return "Steam DRM"
    elif "none" in drm_lower or "drm-free" in drm_lower:
        return "DRM-Free"
    else:
        return "Custom DRM"


class RateLimiter:
    """Simple rate limiter for API calls"""
    
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = []
    
    def is_allowed(self) -> bool:
        """Check if request is allowed under rate limit"""
        import time
        now = time.time()
        
        # Remove old requests outside window
        self.requests = [req_time for req_time in self.requests if now - req_time < self.window_seconds]
        
        # Check if under limit
        if len(self.requests) < self.max_requests:
            self.requests.append(now)
            return True
        
        return False
    
    def time_until_allowed(self) -> float:
        """Get seconds until next request is allowed"""
        if not self.requests:
            return 0.0
        
        import time
        oldest = min(self.requests)
        return max(0.0, self.window_seconds - (time.time() - oldest))
