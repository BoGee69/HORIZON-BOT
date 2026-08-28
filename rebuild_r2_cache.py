#!/usr/bin/env python3
"""Rebuild R2 inventory SQLite cache from live R2 listing."""
import sys, time, logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("rebuild_r2_cache")

import config as bot_config
import boto3
from utils.database import R2InventoryDB

session = boto3.session.Session()
client = session.client(
    "s3",
    endpoint_url=f"https://{bot_config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
    aws_access_key_id=bot_config.R2_ACCESS_KEY_ID,
    aws_secret_access_key=bot_config.R2_SECRET_ACCESS_KEY,
    region_name="auto",
)

db = R2InventoryDB()
log.info("Rebuilding R2 inventory cache from live bucket...")
start = time.time()
result = db.rebuild(client, bot_config.R2_BUCKET_NAME, bot_config.R2_MAINTENANCE_PREFIX)
elapsed = time.time() - start
log.info("Done: %d keys synced in %.1fs", result["keys_synced"], elapsed)
