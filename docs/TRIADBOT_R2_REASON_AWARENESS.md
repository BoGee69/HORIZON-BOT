# TriadBot R2 Reason Awareness Patch

Patch ini membuat pertanyaan seperti:

- `kenapa masih ada yang belum di rename?`
- `alasan zip belum rapi apa?`
- `kok masih ada AppID-only?`

menjadi jawaban read-only yang menjelaskan alasan, bukan hanya angka.

TriadBot akan menjelaskan kemungkinan penyebab:

- file masih `AppID.zip` dan belum kena batch maintenance terbaru;
- nama game belum ditemukan di SQLite/cache/Steam API saat run;
- Steam lookup gagal atau AppID masuk blacklist sementara;
- target `Nama Game (AppID).zip` sudah ada sehingga rename dilewati;
- format file tidak punya AppID yang bisa diparse;
- data yang dipakai berasal dari cache inventory sehingga belum berisi diagnosis per-file lengkap.

Jika ada `last_r2_maintenance_summary`, TriadBot juga akan menampilkan angka skipped, Steam lookup failed, blacklist, error, dan contoh alasan dari run terakhir.
