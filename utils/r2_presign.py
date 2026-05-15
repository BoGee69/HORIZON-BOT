"""
Cloudflare R2 Presigned URL generator - FIXED
"""

import asyncio
import logging
from typing import Optional

from config import (
    R2_ACCESS_KEY_ID,
    R2_SECRET_ACCESS_KEY,
    R2_ACCOUNT_ID,
    R2_BUCKET_NAME,
    LINK_EXPIRE_SECONDS,
)

log = logging.getLogger(__name__)

_PRESIGN_ENABLED = all([
    R2_ACCESS_KEY_ID, 
    R2_SECRET_ACCESS_KEY, 
    R2_ACCOUNT_ID, 
    R2_BUCKET_NAME
])

def _make_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", retries={'max_attempts': 3}),
        region_name="auto",   # ← PENTING
    )

def _generate_sync(object_key: str, expires_in: int) -> Optional[str]:
    try:
        client = _make_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": object_key},
            ExpiresIn=expires_in,
        )
        log.info(f"✅ Presigned URL OK: {object_key}")
        return url
    except Exception as exc:
        log.error(f"R2 presign error for '{object_key}': {exc}")
        return None

async def generate_presigned_url(
    object_key: str,   # Bisa full path seperti "Database/12345.zip"
    expires_in: int = LINK_EXPIRE_SECONDS,
) -> Optional[str]:
    if not _PRESIGN_ENABLED:
        log.warning("R2 presign disabled (credential tidak lengkap)")
        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_sync, object_key, expires_in)