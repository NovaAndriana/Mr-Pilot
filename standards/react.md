# Frontend React + TypeScript Standard

## Tooling (wajib hijau di CI)
- ESLint, Prettier, `tsc --noEmit` (strict mode), unit test, dan component test.

## TypeScript
- Hindari `any`. Gunakan tipe spesifik, `unknown` + type guard, atau generic.
- Tidak ada `@ts-ignore`. Kalau terpaksa, pakai `@ts-expect-error` dengan alasan.
- Tipe response API didefinisikan di satu tempat (mis. `src/types/api`), tidak diketik ulang di tiap komponen.

## Komponen
- Komponen fokus satu hal. Lebih dari ±250 baris sebaiknya dipecah.
- Tidak ada logika fetch langsung di komponen presentational. Gunakan hook/service.
- Key pada list memakai id stabil, bukan index array.

## Error handling
- Setiap request API menangani state loading, error, dan kosong.
- Error ditampilkan dengan pesan yang dimengerti user. Detail teknis hanya ke logger.

## API handling
- Semua request lewat satu API client (base URL, auth header, interceptor error). Tidak ada `fetch`/`axios` mentah di komponen.
- Base URL dan key dari env (`import.meta.env` / `process.env`), bukan hardcode.

## Lain-lain
- Tidak ada `console.log` di kode yang di-merge.
- Tidak ada `dangerouslySetInnerHTML` tanpa sanitasi.
