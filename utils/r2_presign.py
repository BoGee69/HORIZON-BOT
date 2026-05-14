"""
Cloudflare R2 Presigned URL generator
Produces time-limited, single-use-style download links for R2 objects.

Requirements
------------
pip install boto3

Cloudflare setup
----------------
1. Cloudflare Dashboard → R2 → your bucket → Settings → make bucket **private**
2. Cloudflare Dashboard → R2 → Manage R2 API Tokens → Create API Token
   (permission: Object Read, scope: specific bucket)
3. Copy the Access Key ID and Secret Access Key into .env:
       R2_ACCESS_KEY_ID=...
       R2_SECRET_ACCESS_KEY=...
       R2_ACCOUNT_ID=...          ← from R2 overview page URL
       R2_BUCKET_NAME=...
       LINK_EXPIRE_SECONDS=3600   ← default 1 hour

If the credentials are not set the module returns None and game_commands.py
falls back to the plain public R2_BASE_URL.
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

# Only import boto3 when credentials are actually configured
_PRESIGN_ENABLED = all([R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ACCOUNT_ID, R2_BUCKET_NAME])


def _make_client():
    """Create a boto3 S3 client pointed at the Cloudflare R2 endpoint."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _generate_sync(object_key: str, expires_in: int) -> Optional[str]:
    """Synchronous presigned-URL generation (runs in a thread pool)."""
    try:
        client = _make_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET_NAME, "Key": object_key},
            ExpiresIn=expires_in,
        )
        return url
    except Exception as exc:
        log.error(f"R2 presign error for key '{object_key}': {exc}")
        return None


async def generate_presigned_url(
    appid: str,
    expires_in: int = LINK_EXPIRE_SECONDS,
) -> Optional[str]:
    """
    Async wrapper — generates a presigned download URL for Database/<appid>.zip.

    Returns None when:
    - R2 credentials are not configured  →  caller should fall back to public URL
    - boto3 is not installed
    - The presign operation itself fails
    """
    if not _PRESIGN_ENABLED:
        return None

    object_key = f"Database/{appid}.zip"
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_sync, object_key, expires_in)
