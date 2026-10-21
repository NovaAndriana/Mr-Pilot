# Standar Frontend (React + TypeScript)

Aturan umum di `general.md` juga berlaku.

## Tooling (wajib hijau di CI)
- ESLint (termasuk `react-hooks`), Prettier, `tsc --noEmit` (strict), unit test dan component test.

## Struktur & penamaan
- Wajib: folder per fitur: `src/api` (satu HTTP client), `src/types/api` (tipe API), `src/components` (lintas fitur), `src/features/<fitur>/{components,hooks,services}`.
- Wajib: komponen PascalCase, satu komponen utama per file dengan nama yang sama; hook diawali `use`; boolean diawali `is`/`has`/`can`; handler `handleXxx`, prop callback `onXxx`.
- Disarankan: import memakai alias (`@/features/quota`) daripada `../../../`.

## TypeScript & komponen
- Wajib: tidak memakai `any`. Gunakan tipe spesifik, `unknown` + type guard, atau generic.
- Wajib: tidak ada `@ts-ignore`. Kalau terpaksa, `@ts-expect-error` dengan alasan.
- Wajib: tipe response API didefinisikan sekali di `src/types/api`, tidak diketik ulang di tiap komponen. Props diberi tipe eksplisit.
- Wajib: key list memakai id stabil, bukan index array.
- Wajib: aturan hooks dipatuhi: tidak dipanggil di dalam kondisi/loop, dependency array lengkap.
- Disarankan: komponen fokus satu hal, di atas ±250 baris dipecah. Fetch dan transformasi data di hook/service, komponen hanya menampilkan.
- Disarankan: nilai turunan dihitung saat render, tidak disimpan di state lalu disinkronkan dengan `useEffect`.

## Data & API
- Wajib: semua request lewat satu API client di `src/api` (base URL, auth header, refresh token, interceptor error). Tidak ada `fetch`/`axios` mentah di komponen.
- Wajib: base URL dan key dari env (`import.meta.env` / `process.env`), bukan ditulis di kode.
- Wajib: setiap request menangani state loading, error, dan data kosong; pesan error bisa dimengerti user, detail teknis ke logger.
- Wajib: field baru dari backend diperlakukan opsional sampai backend-nya rilis ke semua environment.
- Disarankan: data server dikelola dengan library data fetching proyek (cache, retry), bukan `useEffect` + `useState` manual di tiap halaman. State global hanya untuk yang dipakai lintas halaman.

## Keamanan
- Wajib: tidak ada `dangerouslySetInnerHTML` tanpa sanitasi (DOMPurify).
- Wajib: tidak ada secret di kode frontend; semua yang ada di bundle bisa dibaca siapa pun.
- Wajib: token tidak disimpan di `localStorage` kalau proyek memakai cookie httpOnly; ikuti mekanisme auth yang ada.
- Wajib: pengecekan role di UI hanya untuk tampilan; hak akses tetap dicek di backend.
- Wajib: tidak ada `console.log` di kode yang di-merge.

## Aksesibilitas & performa
- Disarankan: elemen yang bisa diklik memakai `<button>`/`<a>`, bukan `<div onClick>`. Input punya label, gambar punya `alt`.
- Disarankan: halaman besar di-lazy load per route; tabel/list panjang memakai pagination atau virtualisasi; library besar tidak di-import utuh.

## Test
- Wajib: hook dan fungsi logika punya unit test. Komponen dengan logika penting dites dari sisi user (Testing Library).
- Wajib: bug fix disertai test yang gagal sebelum perbaikan.
- Disarankan: request API di-mock di level network (MSW).
