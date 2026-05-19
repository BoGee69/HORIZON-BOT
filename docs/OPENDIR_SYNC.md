# Open Directory Sync to Cloudflare R2

This module adds `cogs/opendir_sync.py`, a background-only Discord cog that syncs files from an authorized Open Directory into Cloudflare R2 without saving downloaded files to local disk.

## Safety model

- Disabled by default: `OPENDIR_SYNC_ENABLED=false`.
- Use only on a directory you own or have explicit permission to mirror.
- Files are streamed from `aiohttp` into `boto3.upload_fileobj()` through an in-memory queue.
- No slash commands are added.
- Existing R2 objects are skipped.
- The sync is scoped to the configured base URL host/path and will not follow external links.
- Allowed extensions, depth, file size, concurrency, and interval are configurable.

## Required Railway variables

```env
OPENDIR_SYNC_ENABLED=true
OPENDIR_BASE_URL=https://www.depotgame.my.id/
OPENDIR_R2_PREFIX=Database/
OPENDIR_INTERVAL_HOURS=6
OPENDIR_RUN_ON_START=true
OPENDIR_START_DELAY_SECONDS=20
OPENDIR_MAX_DEPTH=3
OPENDIR_MAX_FILES_PER_RUN=20
OPENDIR_MAX_FILE_MB=1024
OPENDIR_CONCURRENCY=2
OPENDIR_ALLOWED_EXTENSIONS=zip,manifest,lua,acf,vdf
```

For the first production test, keep `OPENDIR_MAX_FILES_PER_RUN=20`. After logs look clean, increase it or set `0` for no explicit cap.

## URL configuration note

Use the real domain URL:

```env
OPENDIR_BASE_URL=https://www.depotgame.my.id/
```

Do not use the Cloudflare edge IP directly, for example `http://172.67.202.94/`. Accessing a Cloudflare-backed site by IP often returns HTTP 403 because the Host/SNI does not match the configured domain.

## Existing R2 variables still required

```env
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_ACCOUNT_ID=...
R2_BUCKET_NAME=...
```

## Runtime behavior

1. When the bot starts, the cog waits until Discord is ready.
2. If `OPENDIR_RUN_ON_START=true`, it performs an initial full scan.
3. It lists existing R2 keys under `OPENDIR_R2_PREFIX`.
4. It scans the open directory recursively up to `OPENDIR_MAX_DEPTH`.
5. It streams only missing files to R2.
6. It sleeps for `OPENDIR_INTERVAL_HOURS`, then checks again for new files.

## Notes

- The module writes summary events into `bot.record_ai_event()` when available.
- Admin notifications are sent only on errors by default. Set `OPENDIR_NOTIFY_ON_SUCCESS=true` if you want success notifications too.
- `OPENDIR_FLATTEN_R2_KEYS=false` preserves source subfolder paths under the R2 prefix. Set it to `true` only if every filename is globally unique.
