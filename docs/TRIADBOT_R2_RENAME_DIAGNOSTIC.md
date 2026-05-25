# TriadBot R2 Rename Diagnostic

Patch ini menambahkan diagnostic read-only agar TriadBot bisa menjawab pertanyaan seperti:

- `kenapa masih ada yang belum di rename?`
- `diagnosa sisa rename`
- `alasan zip belum rapi apa?`
- `analisis zip yang belum rapi berdasarkan bukti`

## Cara kerja

Diagnostic tidak melakukan copy, delete, upload, rewrite ZIP, atau rename file. Diagnostic hanya membaca:

1. R2 inventory cache SQLite, kalau tersedia.
2. Live R2 list-object fallback, kalau cache kosong.
3. SQLite games table dan Steam/cache name map.
4. R2 maintenance state untuk Steam lookup failure / blacklist evidence.
5. Target file existence untuk membuktikan apakah target rename sudah ada.

## Reason yang bisa dibuktikan

TriadBot akan mengelompokkan sisa ZIP ke reason seperti:

- `no_appid`: AppID tidak bisa diparse dari nama file.
- `missing_game_name`: AppID ada, tetapi nama game tidak ditemukan di SQLite/cache/R2 name map.
- `steam_failed_before`: AppID punya riwayat Steam lookup gagal.
- `steam_blacklisted`: AppID masuk blacklist Steam lookup sementara.
- `unsafe_game_name`: nama game ada, tetapi tidak aman/valid untuk nama file.
- `target_exists`: target `Nama Game (AppID).zip` sudah ada di R2.
- `rename_possible_now`: data cukup untuk rename; kemungkinan belum kena batch/apply run.

## Railway variables

```env
AI_CHAT_R2_DIAGNOSTIC_ENABLED=true
AI_CHAT_R2_DIAGNOSTIC_ON_WHY=true
AI_CHAT_R2_DIAGNOSTIC_LIMIT=500
AI_CHAT_R2_DIAGNOSTIC_TIMEOUT_SECONDS=25
AI_CHAT_R2_DIAGNOSTIC_USE_CACHE=true
AI_CHAT_R2_DIAGNOSTIC_LIVE_FALLBACK=true
AI_CHAT_R2_DIAGNOSTIC_LIVE_SCAN_LIMIT=1000
AI_CHAT_R2_DIAGNOSTIC_TARGET_EXISTS_CHECKS=100
```

## Test

DM owner/admin:

```txt
kenapa masih ada yang belum di rename?
```

Expected behavior:

- TriadBot menampilkan inventory count.
- TriadBot menjalankan diagnostic read-only.
- TriadBot menjawab reason berdasarkan bukti, bukan asumsi.
- Kalau bukti belum cukup, TriadBot menyebut batas analisisnya.

## Catatan penting

Kalau `Source` masih `live-r2-list-objects` dan `full inventory available: false`, berarti diagnostic hanya sample terbatas. Untuk bukti paling akurat, jalankan/rebuild R2 inventory cache supaya `Source` menjadi `sqlite-r2-inventory-cache`.
