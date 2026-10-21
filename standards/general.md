# Standar Kode AkuSign — Umum

Berlaku untuk semua stack (Backend Go, Frontend React, Mobile React Native).
**Wajib** = MR tidak di-merge kalau dilanggar. **Disarankan** = dibahas saat review, tidak memblokir.

## Penamaan & struktur
- Wajib: nama variabel, fungsi, dan file menjelaskan maksudnya. Singkatan hanya yang umum (id, url, ctx, err).
- Disarankan: hindari nama generik seperti `data`, `temp`, `obj`, `handle2`, `newFunc`.
- Disarankan: satu fungsi satu tanggung jawab. Fungsi di atas ±60 baris atau nesting lebih dari 3 level dipecah.
- Wajib: tidak ada kode duplikat hasil copy-paste, kode mati, kode yang di-comment-out, atau import yang tidak dipakai.

## Error handling
- Wajib: error tidak diabaikan diam-diam. Error ditangani, di-wrap dengan konteks, atau dikembalikan.
- Wajib: pesan error ke user tidak membocorkan detail internal (stack trace, query SQL, path server).

## Logging
- Wajib: pakai logger terstruktur proyek, bukan print/console.
- Wajib: tidak me-log data sensitif: password, token, NIK, nomor rekening, isi dokumen, data pribadi lengkap.
- Disarankan: level log sesuai: `error` untuk kegagalan yang butuh tindakan, `info` untuk event bisnis, `debug` untuk detail teknis.

## Secret & konfigurasi
- Wajib: tidak ada secret, password, API key, private key, atau connection string di kode. Semua lewat env var atau secret manager.
- Wajib: nilai yang beda per environment (URL, timeout, feature flag) masuk konfigurasi, bukan di-hardcode.
- Wajib: env var baru dicatat di `.env.example` dan config deploy.

## Kompatibilitas antar tim
- Wajib: perubahan kontrak API (field dihapus/di-rename, tipe berubah, array jadi object, field wajib baru di request) adalah breaking change: ditandai di MR dan dikoordinasikan dengan BE, FE, dan Mobile.
- Wajib: app mobile versi lama masih dipakai user. Field baru di response bersifat tambahan, field lama dipertahankan sampai versi lama tidak dipakai lagi.

## Dokumentasi
- Disarankan: fungsi/endpoint yang tidak jelas dari namanya diberi komentar singkat tentang *kenapa*, bukan *apa*.
- Wajib: endpoint baru atau berubah diperbarui di Swagger/Postman di MR yang sama.

## Git convention
- Wajib: judul MR dan pesan commit `type(TIKET): deskripsi`, contoh `feat(IDAS-5323): show per user quota usage`. Tanda `!` sebelum `:` untuk breaking change.
- type: feat, fix, refactor, perf, test, docs, chore, ci, build, revert.
- Wajib: nama branch `type/TIKET-deskripsi-singkat`, contoh `feat/IDAS-5413-quota-bundle`. Satu branch satu tiket.
- Wajib: TODO menyebut tiket: `// TODO(IDAS-123): ...`.
- Disarankan: satu MR satu tiket, usahakan di bawah ±400 baris perubahan (di luar lock file, file generate, dan mock).
- Wajib: kalau MR bergantung pada MR lain, tulis di deskripsi `Harus di-merge setelah: !123`.
