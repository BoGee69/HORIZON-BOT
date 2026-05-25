# TriadBot Guardian Mode

Guardian Mode membuat TriadBot lebih terasa seperti asisten operasional, security monitor, dan caretaker server/database.

## Mode utama

### Assistant
- Menjawab pertanyaan owner/admin soal bot, server, SQLite, R2, OpenDir, Steam DB sync, dan error runtime.
- Pertanyaan biasa seperti `jam berapa?` dijawab sebagai pertanyaan waktu, bukan dipaksa masuk konteks R2/database.
- Pertanyaan status seperti `progres rename`, `status R2`, `cek database`, dan `kondisi server aman?` dibalas dari live state.

### Security
- Cog baru: `cogs/ai_security.py`.
- Memantau message spam, mention flood, dan link flood.
- Default: alert-only. Tidak delete/timeout otomatis kecuali env auto-action diaktifkan.
- Security snapshot masuk ke prompt AI sebagai `security_guardian`.

### Caretaker
- Guardian report menggabungkan health check, SQLite, R2 inventory, security snapshot, job terakhir, dan proposal pending.
- Owner/admin bisa DM:
  - `kondisi server aman?`
  - `guardian report`
  - `laporan caretaker`
  - `cek semuanya`

## Railway env yang disarankan

```env
BOT_TIMEZONE=Asia/Jakarta
AI_CHAT_TEMPERATURE=0.25
AI_CHAT_MAX_HISTORY=30
AI_CHAT_MEMORY_PERSIST=true
AI_CHAT_MEMORY_PATH=data/ai_chat_memory.json

AI_SECURITY_ENABLED=true
AI_SECURITY_ALERT_ONLY=true
AI_SECURITY_AUTO_DELETE_SPAM=false
AI_SECURITY_AUTO_TIMEOUT_SPAM=false
AI_SECURITY_WINDOW_SECONDS=12
AI_SECURITY_MAX_MESSAGES_PER_WINDOW=7
AI_SECURITY_MAX_MENTIONS_PER_MESSAGE=8
AI_SECURITY_MAX_LINKS_PER_WINDOW=4
AI_SECURITY_ALERT_COOLDOWN_SECONDS=180
AI_SECURITY_TIMEOUT_SECONDS=600
```

## Auto-action security

Jangan nyalakan auto-action sebelum bot permissions dan false-positive sudah aman.

Mode aman default:

```env
AI_SECURITY_ALERT_ONLY=true
AI_SECURITY_AUTO_DELETE_SPAM=false
AI_SECURITY_AUTO_TIMEOUT_SPAM=false
```

Mode lebih agresif:

```env
AI_SECURITY_ALERT_ONLY=false
AI_SECURITY_AUTO_DELETE_SPAM=true
AI_SECURITY_AUTO_TIMEOUT_SPAM=false
```

Timeout otomatis sebaiknya tetap off sampai rules server dan whitelist role sudah jelas.

## Approval tetap wajib

Guardian Mode tidak memberi AI kuasa bebas untuk mengubah server/database. Aksi write tetap melalui operator approval:

```text
Approval required
Proposal ID: abc123
```

Owner menjalankan dengan:

```text
approve abc123
```

## Incident-aware follow-up

Patch ini membuat pertanyaan pendek seperti `ada apa?`, `ini apa?`, `warning apa?`, `kenapa warning?`, dan `apa masalahnya?` membaca sumber internal terlebih dahulu:

- `last_ai_caretaker_result`
- event buffer `bot.ai_events`
- security recent alerts
- bad health checks

Tujuannya agar bot tidak menjawab `tidak ada masalah` saat di atasnya ada pesan `WARNING` dari caretaker atau event runtime.
