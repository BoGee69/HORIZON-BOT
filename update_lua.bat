@echo off
cd /d "%~dp0"
python lua_sync.py --apply --limit 5000 >> logs\lua_sync.log 2>&1
echo [%date% %time%] Done >> logs\lua_sync.log
