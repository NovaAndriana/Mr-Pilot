# Backend Go Standard

## Tooling (wajib hijau di CI)
- `gofmt` / `goimports`, `go vet`, `golangci-lint` (config di repo), dan unit test dengan `-race`.
- Mock dibuat dengan **Mockery** dari interface, jangan ditulis manual.

## Struktur package
- Ikuti layer: `internal/<domain>` (usecase/business logic), `internal/repo` (data access), `pkg/api` (transport/handler).
- Handler tidak boleh mengakses repository langsung. Handler → usecase → repository.
- Usecase bergantung pada interface, bukan implementasi konkret.

## Error handling
- Jangan abaikan error (`_ = fn()` atau `x, _ := fn()`) kecuali ada komentar alasannya.
- Wrap error dengan konteks: `fmt.Errorf("get workflow %s: %w", id, err)`.
- Error domain didefinisikan sebagai variabel (`var ErrWorkflowNotFound = errors.New(...)`) dan dipetakan ke kode error API di satu tempat.
- Tidak ada `panic` di kode aplikasi, kecuali saat startup (`main`/init config).

## Logging
- Pakai logger terstruktur proyek (zap/zerolog/slog) dengan field, bukan `fmt.Print*` atau `log.Print*`.

## Database
- Query repository selalu punya filter. Query tanpa WHERE ke tabel besar dianggap **blocker**.
- Tidak ada string concatenation untuk SQL. Gunakan parameter/ORM.
- Operasi multi-tabel yang harus atomik memakai transaksi.

## API response
- Format response konsisten: `{ "data": ..., "error": { "code": "...", "message": "..." } }`.
- Perubahan bentuk response (field dihapus, tipe berubah, array jadi object) = **breaking change**, wajib ditandai di MR.

## Konfigurasi
- Config dibaca sekali saat startup ke struct, bukan `os.Getenv` tersebar di banyak file.

## Test
- Usecase baru/berubah wajib ada unit test, sebaiknya table-driven.
- Test edge case: input kosong, nil, not found, dan unauthorized.
