"""
Utilities package for bot
"""
from .database import DatabaseManager
from .helpers import (
    clean_search_string,
    make_safe_filename,
    extract_appid_from_filename,
    format_size,
    format_number,
    truncate_text,
    is_valid_appid,
    parse_github_url,
    is_valid_github_url,
    extract_protection_type,
    RateLimiter
)
from .steam_api import SteamAPI

__all__ = [
    'DatabaseManager',
    'clean_search_string',
    'make_safe_filename',
    'extract_appid_from_filename',
    'format_size',
    'format_number',
    'truncate_text',
    'is_valid_appid',
    'parse_github_url',
    'is_valid_github_url',
    'extract_protection_type',
    'RateLimiter',
    'SteamAPI'
]
