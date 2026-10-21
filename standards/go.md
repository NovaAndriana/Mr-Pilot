# Standar Backend (Go)

Aturan umum di `general.md` juga berlaku.

## Tooling (wajib hijau di CI)
- `gofmt` / `goimports`, `go vet`, `golangci-lint` (config di repo), dan `go test -race ./...`.
- Mock dibuat dengan **Mockery** dari interface, tidak ditulis manual.

## Struktur & penamaan
- Wajib: alur Handler → Usecase → Repository. Handler tidak memanggil repository langsung, repository tidak berisi logika bisnis.
- Wajib: layout `pkg/api` (handler/transport), `internal/<domain>` (usecase, entity, interface repository), `internal/repo` (implementasi akses data). Service dengan nama layer lain (entity, usecase, adapter, impl) tetap mengikuti alur yang sama.
- Wajib: usecase bergantung pada interface, bukan struct konkret.
- Wajib: package huruf kecil satu kata tanpa underscore; file snake_case; exported PascalCase, unexported camelCase; singkatan seragam huruf besar (`userID`, `docURL`, `HTTPClient`, bukan `userId`); constructor `NewXxx`.

## Error, logging & context
- Wajib: error tidak diabaikan dengan `_` kecuali ada komentar alasannya.
- Wajib: wrap error dengan konteks `fmt.Errorf("get quota %s: %w", id, err)`; cek dengan `errors.Is` / `errors.As`, bukan membandingkan string.
- Wajib: error domain sebagai variabel (`var ErrQuotaNotFound = errors.New(...)`) dan dipetakan ke kode error API di satu tempat.
- Wajib: tidak ada `panic` di kode aplikasi, kecuali saat startup (`main`, load config).
- Wajib: logger terstruktur proyek dengan field, bukan `fmt.Print*` / `log.Print*`.
- Wajib: `context.Context` jadi parameter pertama di usecase dan repository, diteruskan sampai query DB dan HTTP call. Tidak membuat `context.Background()` / `context.TODO()` di tengah alur request.
- Disarankan: error di-log sekali di boundary (handler/worker), bukan log lalu return di setiap layer.
- Disarankan: goroutine punya cara berhenti lewat `ctx` dan error-nya ditangkap (mis. `errgroup`).

## Database
- Wajib: SQL memakai parameter (`$1`, `?`) atau query builder, tidak disusun dengan menggabungkan string.
- Wajib: query ke tabel besar selalu punya filter dan pagination/LIMIT. Query tanpa WHERE ke tabel besar = blocker.
- Wajib: operasi multi-tabel yang harus atomik memakai transaksi dengan rollback di `defer`.
- Wajib: tidak ada query di dalam loop (N+1). Ambil sekaligus, mis. `WHERE id = ANY($1)`.
- Disarankan: kolom yang dipakai filter di query baru diberi index dan disebut di MR.

## Migrasi (golang-migrate)
- Wajib: setiap migrasi punya pasangan `.up.sql` dan `.down.sql`, nomor versi unik (`migrate create -ext sql -dir db/migrations <nama>`).
- Wajib: migrasi yang sudah pernah jalan di environment mana pun tidak diubah isinya atau dinomori ulang; perubahan dibuat sebagai migrasi baru.
- Wajib: seed idempotent (`ON CONFLICT DO NOTHING` / `WHERE NOT EXISTS`); seed yang gagal membuat `schema_migrations` dirty.
- Wajib: DROP kolom/tabel dua tahap: rilis kode yang tidak memakainya dulu, DROP di rilis berikutnya.
- Wajib: fungsi yang bergantung versi database (mis. `uuidv7()` butuh PostgreSQL 18) dicek terhadap versi DB di semua environment.
- golang-migrate hanya menjalankan versi yang lebih besar dari versi terakhir di `schema_migrations`; kalau MR lain dengan versi lebih besar sudah di-merge duluan, beri nomor baru sebelum merge.

## API & keamanan
- Wajib: format response `{ "data": ..., "error": { "code": "...", "message": "..." } }`.
- Wajib: input divalidasi di handler (wajib/opsional, panjang, format, enum) sebelum masuk usecase; input tidak valid dibalas 400 dengan pesan jelas.
- Wajib: hak akses dicek di server: resource yang diminta benar milik user/tenant yang login. ID dari client tidak dipercaya begitu saja.
- Wajib: config dibaca sekali saat startup ke struct, bukan `os.Getenv` tersebar.
- Wajib: HTTP client ke layanan luar punya timeout (bukan `http.Get` / `http.DefaultClient`). `rows` dan `resp.Body` ditutup dengan `defer`.
- Disarankan: endpoint list memakai pagination dengan batas maksimal per halaman.

## Test
- Wajib: usecase baru/berubah punya unit test, sebaiknya table-driven.
- Wajib: edge case dites: input kosong, nil, not found, unauthorized, error dari repository.
- Wajib: bug fix disertai test yang gagal sebelum perbaikan.
- Disarankan: query kompleks dites ke database sungguhan (DB test/testcontainers).
