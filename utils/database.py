"""
Database manager for game database operations
Handles JSON storage, indexing, and backup management
"""
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from config import DB_PATH, DATA_DIR, ENABLE_AUTO_BACKUP, MAX_BACKUPS

log = logging.getLogger(__name__)
PLACEHOLDER_RE = re.compile(r"^game\s+\d+$", re.IGNORECASE)


def is_placeholder_game_name(name: Optional[str], appid: str) -> bool:
    if not name:
        return True
    clean = " ".join(str(name).strip().split())
    return clean == str(appid) or PLACEHOLDER_RE.match(clean) is not None


class DatabaseManager:
    """Manages game database with auto-indexing and backup"""
    
    def __init__(self):
        self.db_path = DB_PATH
        self.game_db: List[Dict] = []
        self.game_index: Dict[str, Dict] = {}
        self.last_appid = 10
        
    def load(self) -> bool:
        """Load database from file"""
        try:
            if self.db_path.exists():
                with open(self.db_path, 'r', encoding='utf-8') as f:
                    self.game_db = json.load(f)
                
                # Build index
                self.game_index = {str(g["id"]): g for g in self.game_db}
                
                # Get max AppID
                ids = [int(g["id"]) for g in self.game_db if str(g["id"]).isdigit()]
                if ids:
                    self.last_appid = max(ids)
                
                log.info(f"✅ Loaded {len(self.game_db):,} games from database")
                return True
            else:
                log.warning(f"Database file {self.db_path} not found, starting fresh")
                self.game_db = []
                self.game_index = {}
                return False
                
        except json.JSONDecodeError as e:
            log.error(f"Failed to parse database JSON: {e}")
            # Try to restore from backup
            if self._restore_from_backup():
                return True
            self.game_db = []
            self.game_index = {}
            return False
            
        except Exception as e:
            log.error(f"Unexpected error loading database: {e}")
            self.game_db = []
            self.game_index = {}
            return False
    
    def save(self) -> bool:
        """Save database to file with optional backup"""
        try:
            # Create backup before saving
            if ENABLE_AUTO_BACKUP:
                self._create_backup()
            
            # Save database
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.db_path, 'w', encoding='utf-8') as f:
                json.dump(self.game_db, f, indent=2, ensure_ascii=False)
            
            # Rebuild index
            self.game_index = {str(g["id"]): g for g in self.game_db}
            
            log.debug(f"Database saved ({len(self.game_db):,} entries)")
            return True
            
        except Exception as e:
            log.error(f"Failed to save database: {e}")
            return False
    
    def add_game(self, appid: str, name: Optional[str] = None, has_file: bool = False) -> bool:
        """Add a game to database"""
        appid_str = str(appid)
        
        if appid_str in self.game_index:
            log.debug(f"Game {appid_str} already exists in database")
            return False
        
        entry = {
            "id": appid_str,
            "name": name,
            "file": has_file
        }
        
        self.game_db.append(entry)
        self.game_index[appid_str] = entry
        
        # Update max AppID if applicable
        if appid_str.isdigit():
            appid_int = int(appid_str)
            if appid_int > self.last_appid:
                self.last_appid = appid_int
        
        return True
    
    def update_game(self, appid: str, **kwargs) -> bool:
        """Update game entry"""
        appid_str = str(appid)
        
        if appid_str not in self.game_index:
            log.warning(f"Game {appid_str} not found in database")
            return False
        
        # Update in index
        self.game_index[appid_str].update(kwargs)
        
        return True
    
    def mark_as_starred(self, appid: str, name: Optional[str] = None) -> bool:
        """Mark game as having file available"""
        appid_str = str(appid)
        
        if appid_str in self.game_index:
            self.game_index[appid_str]["file"] = True
            if name and is_placeholder_game_name(self.game_index[appid_str].get("name"), appid_str):
                self.game_index[appid_str]["name"] = name
        else:
            # Add new entry
            self.add_game(appid_str, name, has_file=True)
        
        return True
    
    def get_game(self, appid: str) -> Optional[Dict]:
        """Get game by AppID"""
        return self.game_index.get(str(appid))
    
    def search_games(self, query: str, limit: int = 25) -> List[Dict]:
        """Search games by name or AppID"""
        query_lower = query.lower()
        results = []
        
        for game in self.game_db:
            if not game.get("name"):
                continue
            
            name_lower = game["name"].lower()
            appid = str(game["id"])
            
            if query_lower in name_lower or query_lower in appid:
                results.append(game)
                
                if len(results) >= limit:
                    break
        
        return results
    
    def get_stats(self) -> Dict:
        """Get database statistics"""
        total = len(self.game_db)
        with_files = sum(1 for g in self.game_db if g.get("file"))
        with_names = sum(1 for g in self.game_db if g.get("name"))
        
        return {
            "total": total,
            "with_files": with_files,
            "with_names": with_names,
            "last_appid": self.last_appid
        }
    
    def _create_backup(self) -> bool:
        """Create timestamped backup of database"""
        if not self.db_path.exists():
            return False
        
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"games_backup_{timestamp}.json"
            backup_path = DATA_DIR / backup_name
            
            shutil.copy2(self.db_path, backup_path)
            log.debug(f"Created backup: {backup_name}")
            
            # Clean old backups
            self._clean_old_backups()
            
            return True
            
        except Exception as e:
            log.error(f"Failed to create backup: {e}")
            return False
    
    def _clean_old_backups(self):
        """Keep only the most recent backups"""
        try:
            backups = sorted(
                DATA_DIR.glob("games_backup_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            
            # Remove old backups
            for old_backup in backups[MAX_BACKUPS:]:
                old_backup.unlink()
                log.debug(f"Removed old backup: {old_backup.name}")
                
        except Exception as e:
            log.error(f"Failed to clean old backups: {e}")
    
    def _restore_from_backup(self) -> bool:
        """Restore database from most recent backup"""
        try:
            backups = sorted(
                DATA_DIR.glob("games_backup_*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            
            if not backups:
                log.warning("No backups found to restore from")
                return False
            
            latest_backup = backups[0]
            log.info(f"Restoring from backup: {latest_backup.name}")
            
            shutil.copy2(latest_backup, self.db_path)
            
            # Try loading again
            with open(self.db_path, 'r', encoding='utf-8') as f:
                self.game_db = json.load(f)
            
            self.game_index = {str(g["id"]): g for g in self.game_db}
            
            log.info(f"✅ Restored {len(self.game_db):,} games from backup")
            return True
            
        except Exception as e:
            log.error(f"Failed to restore from backup: {e}")
            return False
