# Engineering Code Quality Standard — Umum

Berlaku untuk semua stack. Singkat dan bisa dicek. Kalau aturan tidak bisa dicek di code review, jangan taruh di sini.

## Naming
- Nama variabel, fungsi, dan file harus menjelaskan maksudnya. Singkatan hanya untuk yang umum (id, url, ctx, err).
- Hindari nama generik seperti `data`, `temp`, `obj`, `handle2`, `newFunc`.

## Struktur
- Satu fungsi satu tanggung jawab. Fungsi di atas ±60 baris atau nesting lebih dari 3 level sebaiknya dipecah.
- Tidak ada kode duplikat yang di-copy-paste lebih dari sekali. Ekstrak ke helper.
- Tidak ada kode mati, kode yang di-comment-out, atau import yang tidak dipakai.

## Error handling
- Error tidak boleh diabaikan diam-diam. Error ditangani, di-wrap dengan konteks, atau dikembalikan.
- Pesan error ke user tidak membocorkan detail internal (stack trace, query SQL, path server).

## Logging
- Pakai logger terstruktur proyek, bukan print/console.
- Jangan log data sensitif: password, token, nomor KTP, nomor rekening, data pribadi lengkap.
- Level log sesuai: `error` untuk kegagalan yang butuh tindakan, `info` untuk event bisnis, `debug` untuk detail teknis.

## Environment variable & secret
- Tidak ada secret, password, API key, atau connection string di kode. Semua lewat env var atau secret manager.
- Nilai yang beda per environment (URL, timeout, feature flag) masuk ke konfigurasi, bukan di-hardcode.

## Dokumentasi
- Fungsi/endpoint publik yang tidak jelas dari namanya diberi komentar singkat tentang *kenapa*, bukan *apa*.
- Perubahan kontrak API dicatat di deskripsi MR dan dokumentasi API (Swagger/Postman).

## Git convention
- Judul MR dan pesan commit: `type(TICKET): deskripsi`, contoh `feat(IDAS-5323): show per user quota usage`.
- type: feat, fix, refactor, perf, test, docs, chore, ci, build, revert.
- TODO wajib menyebut tiket: `// TODO(IDAS-123): ...`.
