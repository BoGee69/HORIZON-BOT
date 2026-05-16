"""
Cloudflare R2 Presigned URL generator
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

# FIX 4: .strip() mencegah whitespace tersembunyi saat copy-paste di Railway env editor
_KEY     = (R2_ACCESS_KEY_ID     or "").strip()
_SECRET  = (R2_SECRET_ACCESS_KEY or "").strip()
_ACCOUNT = (R2_ACCOUNT_ID        or "").strip()
_BUCKET  = (R2_BUCKET_NAME       or "").strip()

_PRESIGN_ENABLED = all([_KEY, _SECRET, _ACCOUNT, _BUCKET])

if not _PRESIGN_ENABLED:
    log.warning(
        "⚠️  R2 presign DISABLED — cek env variables: "
        f"KEY={'✅' if _KEY else '❌'}  SECRET={'✅' if _SECRET else '❌'}  "
        f"ACCOUNT={'✅' if _ACCOUNT else '❌'}  BUCKET={'✅' if _BUCKET else '❌'}"
    )
else:
    log.info(f"✅ R2 presign ENABLED (bucket: {_BUCKET})")


def _make_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=f"https://{_ACCOUNT}.r2.cloudflarestorage.com",
        aws_access_key_id=_KEY,
        aws_secret_access_key=_SECRET,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        region_name="auto",   # ← Wajib "auto" untuk Cloudflare R2
    )


def _generate_sync(object_key: str, expires_in: int) -> Optional[str]:
    try:
        client = _make_client()
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": _BUCKET, "Key": object_key},
            ExpiresIn=expires_in,
        )
        log.info(f"✅ Presigned URL OK: {object_key}")
        return url
    except Exception as exc:
        log.error(f"❌ R2 presign error untuk '{object_key}': {exc}")
        return None


async def generate_presigned_url(
    object_key: str,
    expires_in: int = LINK_EXPIRE_SECONDS,
) -> Optional[str]:
    if not _PRESIGN_ENABLED:
        log.warning("R2 presign disabled — credential tidak lengkap")
        return None

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _generate_sync, object_key, expires_in)
