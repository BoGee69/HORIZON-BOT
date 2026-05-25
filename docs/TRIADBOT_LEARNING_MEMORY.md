# TriadBot Learning Memory

TriadBot Learning Memory membuat TriadBot bisa belajar dari koreksi Owner/Admin tanpa fine-tuning model.

Fitur ini menyimpan koreksi kecil sebagai rule persistent di SQLite, lalu rule yang relevan dimasukkan lagi ke router dan prompt. Tujuannya supaya TriadBot tidak mengulang kesalahan konteks yang sama setelah restart Railway.

## Cara kerja

1. Owner/Admin mengoreksi jawaban TriadBot lewat DM.
2. TriadBot mendeteksi frasa koreksi seperti `itu salah`, `bukan gitu`, `maksud gw`, `harusnya`, `ingat`, atau `ke depannya`.
3. TriadBot menyimpan rule ke `AI_LEARNING_MEMORY_PATH`.
4. Saat pesan berikutnya masuk, TriadBot mencari rule yang relevan.
5. Rule itu dipakai untuk:
   - menghindari routing action yang salah;
   - memberi konteks tambahan ke prompt AI;
   - menjaga perilaku TriadBot makin nyambung dari waktu ke waktu.

## Contoh

User:

```txt
berapa zip yang belum rapi?
```

TriadBot salah membuat proposal.

Owner mengoreksi:

```txt
itu salah, kalau gw nanya "berapa zip yang belum rapi" maksudnya cuma tanya jumlah, bukan nyuruh maintenance
```

TriadBot menyimpan rule:

```txt
Pertanyaan serupa harus dianggap read-only/status question, bukan proposal/action.
```

Berikutnya:

```txt
berapa zip yang belum rapi?
```

TriadBot harus menjawab angka/status, bukan membuat approval.

## ENV

```env
AI_LEARNING_ENABLED=true
AI_LEARNING_MEMORY_PATH=/data/ai_learning_memory.sqlite3
AI_LEARNING_ALLOW_ADMIN=false
AI_LEARNING_MAX_RULES=300
AI_LEARNING_PROMPT_RULE_LIMIT=8
AI_LEARNING_MIN_MATCH_SCORE=0.12
AI_LEARNING_SUGGEST_PATCH_AFTER_MISTAKES=3
```

Rekomendasi awal: `AI_LEARNING_ALLOW_ADMIN=false`, supaya hanya Owner yang bisa mengajari TriadBot secara permanen.

## Safety

Learning Memory tidak boleh menyimpan rule yang melemahkan keamanan, seperti:

- bypass approval;
- tampilkan token/secret;
- auto-approve action berbahaya;
- hapus semua file tanpa approval;
- edit live code tanpa approval.

Jika koreksi mengandung pola token/secret, TriadBot akan menolak menyimpannya.

## Batasan

Learning Memory bukan fine-tuning. Model dasarnya tidak berubah. Yang berubah adalah lapisan konteks/routing di sekitar model.

Untuk bug yang berulang, alur terbaik tetap:

```txt
Learning rule sementara → TriadBot GitHub patch proposal → Owner approve → PR GitHub → deploy
```
