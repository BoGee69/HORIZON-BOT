# TriadBot Human-Style Replies

Patch ini membuat jawaban TriadBot lebih mirip asisten manusia:

- Jawab inti dulu.
- Status/diagnostic dibuat ringkas secara default.
- Contoh file, log, dan breakdown panjang hanya muncul kalau owner minta `detail`, `lengkap`, `breakdown`, `contoh`, atau `log`.
- Pertanyaan `kenapa` tetap berbasis bukti, tapi tidak dump semua data ke chat.

## Env baru

```env
AI_CHAT_HUMAN_STYLE=true
AI_CHAT_BRIEF_STATUS=true
AI_CHAT_BRIEF_DIAGNOSTIC=true
AI_CHAT_SHOW_EVIDENCE_SAMPLES=false
AI_CHAT_DIAGNOSTIC_MAX_REASONS=4
AI_CHAT_MAX_REPLY_CHARS=1200
```

## Contoh

Owner: `kenapa masih ada rename pending?`

TriadBot akan menjawab singkat:

```txt
Jawaban singkatnya:
Dari 2.000 file yang saya cek, penyebab terbesar adalah AppID punya riwayat Steam lookup gagal: 1.158 file.

Bukti ringkas:
- AppID punya riwayat Steam lookup gagal: 1.158
- Target nama baru sudah ada di R2: 583
- Secara data sekarang bisa di-rename: 257
- Nama game tidak ditemukan: 2

Kalau mau bukti file/log-nya, tanya: detail diagnostic rename.
```
