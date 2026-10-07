# MR Pilot

Asisten review & merge MR GitLab untuk Tech Lead.

```
MR baru (Anda reviewer) ─► review otomatis ─► kartu di Telegram ─► tap ✅ Merge
                                                                    │
                     pesan "sudah di-merge" ke grup IDAS (akun Teams Anda) ◄─ approve + merge di GitLab
```

- **Cek otomatis** tiap 2 menit MR yang reviewer-nya Anda (tanpa webhook, cocok untuk PC kantor).
- **Review** pakai API AI (Claude / OpenAI-compatible) *atau* membaca komentar bot "AI Code Review" yang sudah ada. Mode `bot_then_llm` menunggu bot dulu, lalu fallback ke API.
- **Detail MR** di setiap kartu: 👤 pembuat (nama + username, Jira, waktu), 🎯 masalah yang diselesaikan, 🔧 daftar perubahan, 👍 yang sudah bagus. Sumbernya review AI; kalau AI tidak mengisi, diambil dari deskripsi MR (bagian *Reasons for Change* / *Changes*) dan dari *Highlights* bot review. Kalau terlalu panjang, detail dikirim sebagai pesan terpisah tepat sebelum kartu.
- **Kartu Telegram** berisi verdict, breaking change, temuan, dan cek otomatis (pipeline, conflict, target branch). Tombol: **Merge · Tolak · Review ulang · Buka MR**.
- **Merge aman**: hanya jalan jika pipeline hijau, tidak ada conflict, dan tidak ada commit baru sejak direview. Jika verdict bukan *Approve*, diminta konfirmasi kedua.
- **Tolak**: Anda membalas dengan komentar, lalu diposting ke MR atas nama Anda.
- **Pesan Teams** memakai template kalimat Anda sendiri (dipilih acak), dikirim lewat akun Anda. Tidak ada tanda bot/AI.
- **Code Quality**: standar kode tim (Go, React, React Native, umum) ditulis sendiri. Pelanggaran diberi **warning langsung di commit-nya** (file + baris), diringkas di MR, dan muncul di Telegram serta dashboard.
- **Dashboard realtime**: antrian MR, aktivitas langsung, tren pelanggaran standar, dan editor standar. Buka `http://127.0.0.1:8787`.
- Perintah Telegram: `/status`, `/cek`, `/help`.

---

## 1. Persiapan (sekali saja)

### a. Token GitLab
GitLab → avatar → **Preferences → Access Tokens** → buat token dengan scope **`api`**, beri tanggal kedaluwarsa. Simpan sebagai `GITLAB_TOKEN`.

### b. Bot Telegram
1. Di Telegram chat **@BotFather** → `/newbot` → simpan token sebagai `TELEGRAM_BOT_TOKEN`.
2. Kirim pesan apa saja ke bot baru Anda.
3. Setelah instalasi (langkah 2), jalankan `run.bat --get-chat-id` lalu salin angkanya ke `TELEGRAM_CHAT_ID`.

### c. Flow Teams (supaya pesan tampil atas nama Anda)
1. Buka **make.powerautomate.com** (login akun kantor) → **Create → Instant cloud flow** → *Skip*.
2. Trigger: cari **"When a Teams webhook request is received"**. Who can trigger: *Anyone*.
3. Tambah action **"Post message in a chat or channel"** (Microsoft Teams):
   - **Post as:** `User`
   - **Post in:** `Channel` (pilih Team & channel IDAS) atau `Group chat` (pilih grup IDAS)
   - **Message:** klik *Expression* → `triggerBody()?['text_html']`
4. **Save**, buka lagi trigger-nya, salin URL → simpan sebagai `TEAMS_FLOW_URL`.

> Connection Teams di flow memakai akun Anda, jadi pesan muncul sebagai Anda. Kalau trigger webhook Teams tidak tersedia di tenant, pakai **"When a HTTP request is received"** (mungkin perlu lisensi premium), atau set `teams.mode: telegram_copy`. Teks akan dikirim ke Telegram untuk Anda copy-paste.

### d. API AI (opsional)
Isi `AI_API_KEY` jika memakai mode `llm` / `bot_then_llm`. Untuk gateway internal atau LLM lokal (Ollama, vLLM), pakai `provider: openai` + `base_url`.

---

## 2. Instalasi di komputer kantor (Windows)

1. Install **Python 3.10+** dari python.org (centang *Add Python to PATH*).
2. Ekstrak folder `mr-pilot`, lalu double-click **`setup.bat`**.
3. Isi **`.env`** (token-token di atas) dan sesuaikan **`config.yaml`**:
   - `gitlab.url`, opsional `gitlab.projects`
   - `review.mode` dan `review.bot.usernames` (username akun bot review di MR Anda)
   - `teams.templates`: tulis dengan gaya bahasa Anda sendiri
4. Uji satu per satu (buka *Command Prompt* di folder ini):
   ```
   run.bat --test-telegram
   run.bat --test-teams
   run.bat --review idas/idas-repo-be!375
   run.bat --dry-run
   ```
   `--dry-run` hanya mencetak apa yang akan dikirim, tanpa merge dan tanpa kirim pesan.
5. Jalankan: **`run.bat`**

### Jalan otomatis saat login
Task Scheduler → **Create Task**:
- *General*: "Run only when user is logged on"
- *Triggers*: **At log on**
- *Actions*: Program `C:\path\mr-pilot\run.bat`, *Start in* `C:\path\mr-pilot`
- *Settings*: centang "If the task fails, restart every 1 minute"

Atur juga *Power Options → Sleep: Never* (atau saat dicolok listrik), supaya PC tidak tidur.

---

## 3. Code Quality (standar kode)

Aktifkan di `config.yaml`:
```yaml
code_quality:
  enabled: true
```

### Menulis standar
Semua ada di folder **`standards/`**. Bisa diedit langsung, atau lewat **dashboard → Standar** (ada validasi dan backup `.bak` otomatis).

| File | Isi | Dipakai oleh |
|---|---|---|
| `general.md` | Standar umum: naming, error handling, logging, secret, git convention | AI |
| `go.md`, `react.md`, `react-native.md` | Standar per stack | AI, sesuai stack file yang diubah |
| `pr-checklist.md` | Checklist PR yang wajib ada di deskripsi MR | Cek otomatis |
| `rules.yaml` | Aturan regex yang dicek di **setiap baris yang ditambahkan** | Cek otomatis per commit |
| `ci/gitlab-ci-quality.yml` | Contoh job CI untuk gofmt, go vet, golangci-lint, ESLint, dan tsc | Salin ke repo |

Stack ditentukan dari path file (`code_quality.stacks`). Sesuaikan `paths` dengan struktur repo Anda, misalnya `web/**` untuk FE.

Contoh menambah aturan di `rules.yaml`:
```yaml
  - id: go-no-time-sleep
    stacks: [go]
    severity: warning          # error | warning | info
    pattern: '\btime\.Sleep\('
    message: Hindari time.Sleep di kode aplikasi, gunakan ticker/context.
    exclude_paths: ["*_test.go"]
```
Uji aturan sebelum dipakai lewat dashboard (**Uji aturan otomatis**) atau dari command line:
```
run.bat --check-standards C:\repo\internal\workflow\usecase.go
```

### Apa yang terjadi saat ada MR baru/commit baru
1. **Setiap commit baru** dicek dengan `rules.yaml` dan konvensi pesan commit. Pelanggaran level `warning`/`error` diposting sebagai **komentar di commit tersebut, tepat di baris kodenya**. Setiap commit hanya dicek sekali.
2. **Kondisi MR keseluruhan** dicek: aturan, judul MR, dan PR checklist. Jika `ai_check: true`, AI juga membandingkan diff dengan dokumen `.md` dan hanya temuan dengan keyakinan tinggi yang dipakai, sebagai komentar inline di diff.
3. **Satu komentar ringkasan** di MR yang di-update setiap ada commit baru (tidak spam), ditambah status commit `code-standard`.
4. Kartu Telegram menampilkan jumlah error/warning. Jika ada **error**, tombol Merge meminta konfirmasi kedua.

> Komentar diposting dari akun GitLab Anda. Coba dulu dengan `run.bat --dry-run`, yang hanya mencetak tanpa posting. Kalau terlalu ramai, naikkan `report.min_severity_to_post` ke `error` atau matikan `report.commit_comments`.

`status_fail_on: error` membuat status commit merah jika ada error. Kalau project memakai "Pipelines must succeed", ini **ikut menahan merge**. Default-nya `none` (hanya informasi).

---

## 4. Dashboard

Otomatis jalan bersama `run.bat` di **http://127.0.0.1:8787**.

- **Antrian**: MR yang menunggu keputusan, dengan bar umur (penuh = 24 jam), verdict, error/warning standar, dan pipeline. Klik baris untuk detail lengkap: masalah yang diselesaikan, perubahan, poin bagus, temuan, dan riwayat.
- **Aktivitas langsung**: MR baru, review selesai, warning diposting, merge, dan tolak. Muncul seketika tanpa refresh (Server-Sent Events).
- **Code quality**: tren pelanggaran per hari, aturan paling sering dilanggar, pelanggaran terbuka di MR aktif, per developer (untuk bahan coaching 1:1), dan file paling sering kena.
- **Standar**: edit dokumen dan `rules.yaml`, simpan (divalidasi dulu), lalu uji aturan dengan potongan kode.

Ingin lihat tampilannya dulu tanpa GitLab? Jalankan **`run.bat --demo`** (data contoh, nama fiktif).

Membuka dari HP atau laptop lain di jaringan kantor: set `dashboard.host: 0.0.0.0` dan isi `DASHBOARD_PASSWORD` di `.env`. Tanpa password, dashboard menolak jalan di jaringan.

---

## 5. Keamanan

- Hanya `chat_id` / `allowed_user_ids` yang bisa menekan tombol. Pakai **chat pribadi** dengan bot, jangan grup.
- Token ada di `.env`. Jangan di-commit, jangan dibagikan. Token GitLab sama dengan akses penuh akun Anda.
- Mode `llm` mengirim diff kode ke provider AI. Pastikan sesuai kebijakan perusahaan (atau pakai LLM internal).
- Dashboard hanya bisa diakses dari PC itu sendiri, kecuali Anda membukanya ke jaringan dengan password.
- Semua aksi tercatat di `logs/mr-pilot.log`. Status MR tersimpan di `mr_pilot.db` (hapus file ini untuk reset).

## 6. Troubleshooting

| Gejala | Solusi |
|---|---|
| `SSL: CERTIFICATE_VERIFY_FAILED` ke GitLab | `gitlab.verify_ssl: false` |
| Telegram timeout | Jaringan kantor memblokir Telegram, isi `telegram.proxy` |
| Selalu "menunggu komentar bot" | Cek `review.bot.usernames` / `marker` sesuai akun bot di MR |
| Teams `gagal: HTTP 4xx` | URL flow salah/kedaluwarsa, atau flow nonaktif. Cek *Run history* di Power Automate |
| Merge gagal 405/406 | MR belum mergeable (approval wajib, conflict, discussion belum resolved) |
| Warning tidak muncul di baris commit | Baris tersebut bukan bagian diff commit itu. Ringkasan tetap ada di MR |
| Dashboard tidak bisa dibuka dari HP | `dashboard.host: 0.0.0.0`, isi password, dan izinkan port 8787 di Windows Firewall |

Uji kode: `python -m pytest -q`
