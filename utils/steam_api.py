"""
Steam API client for fetching game data
"""
import asyncio
import logging
from typing import Dict, List, Optional
import aiohttp
from config import STEAM_API_KEY, STEAM_STORE_API, STEAM_SEARCH_API

log = logging.getLogger(__name__)

# Steam Charts API untuk top games
STEAM_CHARTS_API = "https://steamspy.com/api.php?request=top100in2weeks"


class SteamAPI:
    """Client for Steam Store API"""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.api_key = STEAM_API_KEY

    async def get_app_details(self, appid: str, timeout: int = 10) -> Optional[Dict]:
        url = f"{STEAM_STORE_API}?appids={appid}&cc=id&l=english"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    return None
                data = await response.json()
                if not data.get(appid, {}).get("success"):
                    return None
                return data[appid]["data"]
        except asyncio.TimeoutError:
            log.warning(f"Timeout fetching Steam data for AppID {appid}")
            return None
        except Exception as e:
            log.error(f"Error fetching Steam data: {e}")
            return None

    async def search_games(self, query: str, limit: int = 25, timeout: int = 5) -> List[Dict]:
        url = f"{STEAM_SEARCH_API}/?term={query}&l=english&cc=US"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                if response.status != 200:
                    return []
                data = await response.json()
                items = data.get("items", [])[:limit]
                return [
                    {
                        "id": str(item.get("id", "")),
                        "name": item.get("name", ""),
                        "type": item.get("type", ""),
                        "tiny_image": item.get("tiny_image", "")
                    }
                    for item in items
                ]
        except Exception as e:
            log.error(f"Steam search error: {e}")
            return []

    async def get_top_games_steamspy(self, timeout: int = 8) -> List[Dict]:
        """
        Ambil top 100 games dari SteamSpy (berdasarkan pemain aktif 2 minggu terakhir).
        Return list of {appid, name, players}.
        """
        try:
            async with self.session.get(
                STEAM_CHARTS_API, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                results = []
                for appid, info in data.items():
                    results.append({
                        "appid": str(appid),
                        "name": info.get("name", ""),
                        "players": info.get("average_2weeks", 0),
                    })
                # Sort by players descending
                results.sort(key=lambda x: x["players"], reverse=True)
                return results
        except Exception as e:
            log.error(f"SteamSpy top games error: {e}")
            return []

    def extract_game_info(self, steam_data: Dict) -> Dict:
        return {
            "appid": str(steam_data.get("steam_appid", "")),
            "name": steam_data.get("name", "Unknown"),
            "short_description": steam_data.get("short_description", ""),
            "header_image": steam_data.get("header_image", ""),
            "genres": ", ".join([g["description"] for g in steam_data.get("genres", [])]) or "Unknown",
            "developers": ", ".join(steam_data.get("developers", [])) or "Unknown",
            "publishers": ", ".join(steam_data.get("publishers", [])) or "Unknown",
            "release_date": steam_data.get("release_date", {}).get("date", "Unknown"),
            "price": self._get_price(steam_data),
            "drm_notice": steam_data.get("drm_notice", ""),
            "is_free": steam_data.get("is_free", False),
            "type": steam_data.get("type", "game"),
        }

    def _get_price(self, steam_data: Dict) -> str:
        if steam_data.get("is_free"):
            return "Free"
        price_overview = steam_data.get("price_overview", {})
        if price_overview:
            return price_overview.get("final_formatted", "Unknown")
        return "Unknown"
