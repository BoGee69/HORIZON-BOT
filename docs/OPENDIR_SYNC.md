# OpenDir SQLite Sync

`cogs/opendir_sync.py` memakai tabel SQLite `games` sebagai daftar AppID/nama game. OpenDir tidak membaca katalog JSON.

Alur kerja:

1. Bot membaca SQLite dari `SQLITE_PATH`.
2. Untuk setiap game, bot membangun nama target R2:
   - `Database/Nama Game (AppID).zip`
3. Bot mengecek Open Directory untuk kandidat file seperti:
   - `{appid}.zip`
   - `{appid}/{appid}.zip`
   - `Nama Game (AppID).zip`
   - `Nama Game.zip`
4. Jika file ditemukan dan belum ada di R2, bot stream file langsung:
   - `aiohttp` download HTTP
   - queue RAM
   - `boto3.upload_fileobj()` upload ke R2
5. Bot mengirim summary/notifikasi ke admin lewat notifier yang sudah ada.

Tidak ada file yang disimpan ke local disk.

## Environment Penting

```env
OPENDIR_SYNC_ENABLED=true
OPENDIR_BASE_URL=https://www.depotgame.my.id/
OPENDIR_R2_PREFIX=Database/
OPENDIR_STATE_PATH=data/opendir_sync_state.json
OPENDIR_INTERVAL_HOURS=6
OPENDIR_RUN_ON_START=true
OPENDIR_START_DELAY_SECONDS=20

# Untuk test awal, jangan langsung semua.
OPENDIR_GAMES_PER_RUN=500
OPENDIR_MAX_FILES_PER_RUN=20
OPENDIR_CONCURRENCY=1
OPENDIR_NOTIFY_ON_SUCCESS=true

# Biasanya tetap zip karena /gen memakai file ZIP di R2.
OPENDIR_TARGET_EXTENSIONS=zip
OPENDIR_ALLOWED_EXTENSIONS=zip,manifest,lua,acf,vdf
OPENDIR_SOURCE_PATTERNS={appid}.{ext},{appid}/{appid}.{ext},{target_filename},{safe_name}.{ext},{safe_name} ({appid}).{ext}

OPENDIR_INDEX_SCAN_ENABLED=true
OPENDIR_DIRECT_PROBE_ENABLED=true
OPENDIR_USE_HEAD=true
OPENDIR_FALLBACK_GET_PROBE=true
OPENDIR_MAX_FILE_MB=1024
OPENDIR_REQUEST_TIMEOUT_SECONDS=300
OPENDIR_CONNECT_TIMEOUT_SECONDS=30
OPENDIR_READ_TIMEOUT_SECONDS=120
```

## Perbedaan Dengan Scanner Lama

Versi lama hanya membuka halaman root lalu mencari tag `<a href="...zip">`. Kalau URL root bukan directory listing, hasilnya bisa `Files seen: 0`.

Versi ini tetap bisa scan `<a href>`, tetapi juga bisa probe file berdasarkan daftar AppID/nama dari SQLite, misalnya `400.zip`, `620.zip`, atau `Portal (400).zip`.

## State Cursor

Bot menyimpan posisi terakhir di:

```txt
data/opendir_sync_state.json
```

Tujuannya supaya bot tidak harus mengecek seluruh tabel SQLite dalam satu run. Kalau ingin mulai ulang dari awal, hapus file state ini atau set `cursor` ke `0`.

## Catatan Performa

Untuk test awal gunakan:

```env
OPENDIR_GAMES_PER_RUN=100
OPENDIR_MAX_FILES_PER_RUN=10
OPENDIR_CONCURRENCY=1
```

Kalau sudah stabil, naikkan pelan-pelan.

## Output Notifikasi

Notifikasi akan menampilkan:

- total game di SQLite
- jumlah game yang dicek pada run itu
- cursor awal dan cursor berikutnya
- jumlah kandidat URL yang dicek
- jumlah file yang cocok
- jumlah file yang sudah ada di R2
- jumlah file yang berhasil diupload
- sample upload
- error jika ada

Jika muncul `SQLite games table has no valid appid/name records`, berarti `SQLITE_PATH` menunjuk ke database kosong atau tabel `games` belum terisi. Jalankan/aktifkan Steam DB sync atau cek path database.

## Keamanan

Aktifkan modul ini hanya untuk sumber yang kamu miliki atau memang punya izin untuk dimirror. Jangan gunakan untuk bypass login, limit, proteksi, atau aturan situs.
