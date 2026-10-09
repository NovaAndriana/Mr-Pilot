# MR Pilot

Asisten review & merge MR GitLab untuk Tech Lead.

```
MR baru (Anda Reviewer / Assignee) ─► review otomatis ─► kartu di Telegram ─► tap ✅ Merge
                                                                    │
                     pesan "sudah di-merge" ke grup IDAS (akun Teams Anda) ◄─ approve + merge di GitLab
```

- **Cek otomatis** tiap 1 menit semua MR terbuka yang menjadikan Anda **Reviewer atau Assignee** (`gitlab.watch`), termasuk saat Anda baru ditambahkan belakangan. Tanpa webhook, cocok untuk PC kantor. Ketik `/cek` di Telegram untuk cek saat itu juga. MR Draft dikirim begitu statusnya *Ready*.
- **Review** pakai API AI (Claude / OpenAI-compatible) *atau* membaca komentar bot "AI Code Review" yang sudah ada. Mode `bot_then_llm` memakai komentar bot kalau sudah ada; kalau belum, langsung review pakai API AI (menunggu bot hanya jika belum ada provider AI, dan Anda tetap langsung diberi kabar).
- **Source branch tidak dihapus** saat merge (juga kalau centang "Delete source branch" di MR aktif). Kartu punya tombol terpisah **🗑️ Merge + hapus branch** kalau Anda memang ingin menghapusnya. Ubah lewat `MERGE_SOURCE_BRANCH` di `data/.env`: `ask` (bawaan, dua tombol), `keep` (tidak pernah hapus), `delete` (selalu hapus).
- `config.yaml` versi lama diperbarui otomatis saat start (salinan lama: `config.yaml.bak`), hanya baris yang belum pernah Anda ubah.
- **Detail MR** di setiap kartu: 👤 pembuat (nama + username, Jira, waktu), 🎯 masalah yang diselesaikan, 🔧 daftar perubahan, 👍 yang sudah bagus. Sumbernya review AI; kalau AI tidak mengisi, diambil dari deskripsi MR (bagian *Reasons for Change* / *Changes*) dan dari *Highlights* bot review. Kalau terlalu panjang, detail dikirim sebagai pesan terpisah tepat sebelum kartu.
- **Kartu Telegram** berisi verdict, breaking change, temuan, dan cek otomatis (pipeline, conflict, target branch). Tombol: **Merge · Tolak · Review ulang · Buka MR**.
- **Merge aman**: hanya jalan jika pipeline hijau, tidak ada conflict, dan tidak ada commit baru sejak direview. Jika verdict bukan *Approve*, diminta konfirmasi kedua.
- **Tolak**: Anda membalas dengan komentar, lalu diposting ke MR atas nama Anda.
- **Pesan Teams** memakai template kalimat Anda sendiri (dipilih acak), dikirim lewat akun Anda. Tidak ada tanda bot/AI.
- **Code Quality**: standar kode tim (Go, React, React Native, umum) ditulis sendiri. Pelanggaran diberi **warning langsung di commit-nya** (file + baris), diringkas di MR, dan muncul di Telegram serta dashboard.
- **Dashboard realtime**: antrian MR, aktivitas langsung, tren pelanggaran standar, editor standar, dan pengaturan AI.
- **AI multi-provider**: Claude Code (langganan), Anthropic, Gemini, OpenRouter, Groq, OpenAI, dan lokal (Ollama/LM Studio). Ada urutan fallback otomatis dan provider per tugas, semuanya diatur dari dashboard.
- **Jalan di Docker**, satu perintah `setup` untuk PC lokal maupun server, plus **CI/CD** GitHub Actions / GitLab CI yang dikonfigurasi otomatis.
- Perintah Telegram: `/status`, `/cek`, `/help`.

---

## 1. Mulai cepat

Satu perintah memasang Docker (jika belum ada), build, menjalankan wizard konfigurasi, lalu menyalakan MR Pilot.

| Di mana | Perintah |
|---|---|
| **PC Windows** | double-click **`setup.bat`** (atau `setup.bat -Server` agar dashboard bisa dibuka dari HP/laptop lain) |
| **Server Linux** (Ubuntu/Debian/dll.) | `./setup.sh --server` |
| macOS / WSL | `./setup.sh` |

Wizard menanyakan dan **langsung menguji**:
1. **GitLab**: URL + Personal Access Token (scope `api`). Self-signed SSL terdeteksi otomatis.
2. **Telegram**: token bot dari @BotFather. Chat id **terdeteksi otomatis** setelah Anda mengirim pesan ke bot.
3. **AI**: isi satu atau lebih provider (lihat bagian 2). Setiap provider dites.
4. **Teams**: URL flow Power Automate (opsional, lihat 1c).
5. **Code quality** aktif/tidak, dan **password dashboard** dibuat otomatis.

Semua tersimpan di folder **`data/`**: `config.yaml`, `.env` (token), database, log, standar, dan pengaturan AI. Folder ini tidak masuk image maupun git.

### Perintah sehari-hari
```
setup.bat logs        ./setup.sh logs        lihat log
setup.bat status      ./setup.sh status      status container
setup.bat doctor      ./setup.sh doctor      cek koneksi GitLab, Telegram, AI, Teams
setup.bat config      ./setup.sh config      jalankan ulang wizard
setup.bat update      ./setup.sh update      git pull + build + restart
setup.bat stop|start  ./setup.sh stop|start
setup.bat demo        ./setup.sh demo        dashboard dengan data contoh (password: demo)
setup.bat password    ./setup.sh password    lihat password dashboard yang aktif (-Reset / --reset = buat baru)
setup.bat ci          ./setup.sh ci          pasang CI/CD + deploy otomatis (bagian 5)
setup.bat reset       ./setup.sh reset       hapus riwayat MR, aktivitas, statistik & log (minta konfirmasi "RESET")
setup.bat trust-cert  ./setup.sh trust-cert  percayai sertifikat SSL GitLab kantor (error CERTIFICATE_VERIFY_FAILED)
```

**`reset`** cocok setelah masa uji coba. Yang dihapus: riwayat MR, aktivitas, pelanggaran standar, statistik AI, file log, dan riwayat log Docker. Yang **tetap**: `data/.env` (token GitLab/Telegram, API key AI), `config.yaml`, `standards/`, pengaturan AI, dan password dashboard. MR yang masih terbuka dan di-assign ke Anda akan dikirim ulang sebagai kartu baru. Penanda "komentar sudah diposting" sengaja disimpan supaya warning di commit lama tidak diposting dobel ke GitLab; tambahkan `-All` / `--all` kalau ingin benar-benar kosong. `-Yes` / `-y` melewati konfirmasi. Komentar yang sudah ada di GitLab dan pesan lama di Telegram tidak ikut terhapus.
Opsi install: `--with-claude-code` (CLI Claude Code ikut dipasang di image), `--with-ollama[=model]` (AI lokal di Docker), `--port 8787`, `--non-interactive` (semua jawaban dari env, untuk otomasi).

### 1a. Token GitLab (cukup SATU token untuk semua repo)
GitLab → avatar → **Preferences → Access Tokens** → **Add new token** → scope **`api`**, beri tanggal kedaluwarsa.

Ini *Personal* Access Token: berlaku untuk **semua project tempat akun Anda menjadi member** (mis. AkuSign FE, AkuSign Mobile, Backend Suite, akusign/fe), jadi **tidak perlu token per repo**. Jangan pakai *Project Access Token* (token per repo): komentar, approve, dan merge akan tercatat atas nama akun bot project, bukan atas nama Anda.

Mau membatasi ke repo tertentu saja? Isi `gitlab.projects` di `data/config.yaml`, mis. `["akusign/akusign-fe-version-2-0", "idas/backend-suite"]` (path persis seperti di URL GitLab). Kosong = semua repo yang meng-assign Anda.

### 1b. Bot Telegram
Di Telegram chat **@BotFather** → `/newbot` → salin tokennya ke wizard.

### 1c. Flow Teams (supaya pesan tampil atas nama Anda)
1. Buka **make.powerautomate.com** (login akun kantor) → **Create → Instant cloud flow** → *Skip*.
2. Trigger: cari **"When a Teams webhook request is received"**. Who can trigger: *Anyone*.
3. Tambah action **"Post message in a chat or channel"** (Microsoft Teams):
   - **Post as:** `User`
   - **Post in:** `Channel` (pilih Team & channel IDAS) atau `Group chat` (pilih grup IDAS)
   - **Message:** klik *Expression* → `triggerBody()?['text_html']`
4. **Save**, buka lagi trigger-nya, salin URL → tempel di wizard.

> Connection Teams di flow memakai akun Anda, jadi pesan muncul sebagai Anda. Tanpa flow, teks dikirim ke Telegram untuk Anda copy-paste (`TEAMS_MODE=telegram_copy`).

### Tanpa Docker (untuk development)
```
pip install -r requirements.txt
python -m mr_pilot setup        # membuat config.yaml + .env di folder ini
python -m mr_pilot              # jalan
python -m mr_pilot --dry-run    # cek MR tanpa kirim/merge apa pun
```

---

## 2. AI: provider, fallback, dan per tugas

### Cara AI mereview (lebih teliti, lebih sedikit temuan palsu)
1. **Konteks lengkap**: diff dengan nomor baris asli, isi lengkap file yang berubah (diambil dari commit MR), pesan commit, deskripsi MR, dan dokumen standar tim (`standards/*.md`). File kode direview lebih dulu daripada test/dokumen.
2. **Checklist per stack**: kontrak API, security (authz/IDOR, injection, secret di log), Go (error diabaikan, nil, goroutine, transaksi), React/RN (hooks, promise, XSS), DB (migrasi, N+1, index), test.
3. **Wajib bukti**: setiap temuan harus mengutip baris kodenya. Kutipan yang tidak ada di MR dianggap halusinasi: severity diturunkan dan ditandai *belum terbukti*. Nomor baris yang salah dikoreksi otomatis.
4. **Second opinion**: temuan blocker/major dicek ulang oleh AI kedua yang skeptis; yang tidak terbukti dibuang (`review.llm.verify`, 1 panggilan AI tambahan hanya jika ada temuan berat).
5. Kartu Telegram menampilkan risiko, `file:baris`, saran perbaikan (💡), penilaian test, dan pertanyaan untuk author. Logika yang berubah tanpa file test ikut ditandai.

Model besar memberi hasil terbaik (Claude Sonnet, Gemini Pro/Flash). Model kecil/gratis tetap terbantu oleh pengecekan bukti di atas.

Atur di **dashboard → AI**, atau lewat `.env` (wizard mengisinya).

| Provider | Kredensial di `data/.env` | Catatan |
|---|---|---|
| **Claude Code** | `CLAUDE_CODE_OAUTH_TOKEN` | Memakai **langganan** Pro/Max/Team lewat CLI `claude -p`. Di Docker: install dengan `--with-claude-code`, lalu jalankan `claude setup-token` sekali di PC yang sudah login dan tempel token-nya. Di PC tanpa Docker yang sudah login Claude Code, token tidak perlu. |
| **Anthropic API** | `ANTHROPIC_API_KEY` | Default `claude-sonnet-5-5` |
| **Google Gemini** | `GEMINI_API_KEY` | Default `gemini-3.8-flash` |
| **OpenRouter** | `OPENROUTER_API_KEY` | Default `anthropic/claude-sonnet-5.5`; ratusan model lain |
| **Groq** | `GROQ_API_KEY` | Default `openai/gpt-oss-120b`, sangat cepat dan murah untuk cek standar |
| **OpenAI / kompatibel** | `OPENAI_API_KEY` | `base_url` bisa diarahkan ke gateway internal |
| **Lokal** | tanpa key | Ollama (`http://localhost:11434/v1`), LM Studio (`:1234/v1`), vLLM. Dari Docker otomatis lewat `host.docker.internal`; atau `--with-ollama` untuk Ollama di dalam Docker. |

- **Urutan fallback**: provider dicoba dari atas. Jika error, timeout, atau kena rate limit, otomatis pindah ke berikutnya. Atur dengan tombol ▲▼ di dashboard.
- **Provider per tugas**: misalnya *Review MR* memakai Claude Code, *Cek standar kode* memakai Groq.
- **Tes** mengecek koneksi dengan satu panggilan kecil. **Ambil daftar model** membaca model yang tersedia langsung dari provider.
- Bisa **menambah provider** sendiri, misalnya akun OpenRouter kedua atau server Ollama lain.
- API key yang diisi lewat dashboard disimpan di `data/ai_overrides.json` (izin file 600) dan tidak pernah dikirim balik ke browser (hanya 4 karakter terakhir).
- Statistik 7 hari (jumlah panggilan, persentase sukses, rata-rata waktu) dan error terakhir tampil di tab yang sama.

> Kode (diff) dikirim ke provider yang dipakai. Pastikan sesuai kebijakan perusahaan. Kalau tidak boleh keluar jaringan, pakai provider **Lokal**.

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
python -m mr_pilot --check-standards internal/workflow/usecase.go
```

### Apa yang terjadi saat ada MR baru/commit baru
1. **Setiap commit baru** dicek dengan `rules.yaml` dan konvensi pesan commit. Pelanggaran level `warning`/`error` diposting sebagai **komentar di commit tersebut, tepat di baris kodenya**. Setiap commit hanya dicek sekali.
2. **Kondisi MR keseluruhan** dicek: aturan, judul MR, dan PR checklist. Jika `ai_check: true`, AI juga membandingkan diff dengan dokumen `.md` dan hanya temuan dengan keyakinan tinggi yang dipakai, sebagai komentar inline di diff.
3. **Satu komentar ringkasan** di MR yang di-update setiap ada commit baru (tidak spam), ditambah status commit `code-standard`.
4. Kartu Telegram menampilkan jumlah error/warning. Jika ada **error**, tombol Merge meminta konfirmasi kedua.

> Komentar diposting dari akun GitLab Anda. Coba dulu dengan `python -m mr_pilot --dry-run` (atau `docker compose run --rm mr-pilot --dry-run`), yang hanya mencetak tanpa posting. Kalau terlalu ramai, naikkan `report.min_severity_to_post` ke `error` atau matikan `report.commit_comments`.

`status_fail_on: error` membuat status commit merah jika ada error. Kalau project memakai "Pipelines must succeed", ini **ikut menahan merge**. Default-nya `none` (hanya informasi).

---

## 4. Dashboard

Jalan otomatis bersama MR Pilot di **http://127.0.0.1:8787** (atau IP server jika memakai `--server`). Password ada di `data/.env` (`DASHBOARD_PASSWORD`).

- **Antrian**: MR yang menunggu keputusan, dengan bar umur (penuh = 24 jam), verdict, error/warning standar, dan pipeline. Klik baris untuk detail lengkap: masalah yang diselesaikan, perubahan, poin bagus, temuan, dan riwayat.
- **Aktivitas langsung**: MR baru, review selesai, warning diposting, merge, dan tolak. Muncul seketika tanpa refresh (Server-Sent Events).
- **Code quality**: tren pelanggaran per hari, aturan paling sering dilanggar, pelanggaran terbuka di MR aktif, per developer (untuk bahan coaching 1:1), dan file paling sering kena.
- **Standar**: edit dokumen dan `rules.yaml`, simpan (divalidasi dulu), lalu uji aturan dengan potongan kode.

Ingin lihat tampilannya dulu tanpa GitLab? Jalankan **`setup.bat demo`** / `./setup.sh demo` (data contoh, nama fiktif, password `demo`).

Membuka dari HP atau laptop lain di jaringan kantor: install dengan `setup.bat -Server` / `./setup.sh --server`. Dashboard selalu memakai password.

---

## 5. CI/CD (GitHub Actions / GitLab CI)

Sudah tersedia **`.github/workflows/mr-pilot.yml`** dan **`.gitlab-ci.yml`**. Setiap push ke `main`/`master`:

```
test (lint, unit, validasi rules.yaml, end-to-end) → build image → end-to-end pada image Docker → push (GHCR) → deploy via SSH
```
Image hanya di-push kalau sudah lulus test end-to-end. Pull request / MR hanya menjalankan test.

### Konfigurasi otomatis: `setup.bat ci` / `./setup.sh ci`
Jalankan dari PC Anda, di folder repo yang sudah di-push ke GitHub atau GitLab:
1. **Mendeteksi** GitHub atau GitLab dari `git remote origin`.
2. Menanyakan **server tujuan** (host, user, port, folder; default `/opt/mr-pilot`).
3. Membuat **SSH deploy key** khusus, lalu memasangnya di server. Password SSH server ditanya **sekali**.
4. **Menyiapkan server**: memasang Docker jika belum ada, membuat folder, mengunggah `docker-compose.yml` + skrip deploy, dan menyalin `data/config.yaml`, `.env`, standar, serta pengaturan AI dari PC Anda.
5. **Mengisi secrets/variables CI** lewat API: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_PORT`, `DEPLOY_PATH`, `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS`.
   - GitHub: butuh token (fine-grained PAT untuk repo ini, izin **Secrets: read & write**). Secret dienkripsi sesuai API GitHub.
   - GitLab: memakai `GITLAB_TOKEN` jika repo di GitLab yang sama, butuh role **Maintainer**.
6. Lalu `git push`, dan pipeline berjalan.

Deploy di server menjalankan `deploy/remote-deploy.sh`: login registry (token dikirim lewat stdin, tidak terlihat di daftar proses server), `docker compose pull`, `up -d`, lalu menunggu status **healthy** dari Docker. Kalau image baru tidak sehat dalam 3 menit, skrip otomatis **kembali ke image sebelumnya**, menampilkan 80 baris log terakhir, dan job ditandai gagal.

> GitLab Runner butuh executor Docker dengan *privileged* untuk `docker:dind`. Jika runner kantor tidak mengizinkan, ganti job `build` ke Kaniko.

---

## 6. Keamanan

- Hanya `chat_id` / `allowed_user_ids` yang bisa menekan tombol. Pakai **chat pribadi** dengan bot, jangan grup.
- Token ada di `data/.env`. Jangan di-commit, jangan dibagikan. Token GitLab sama dengan akses penuh akun Anda.
- Container berjalan sebagai user biasa (bukan root). Dashboard di Docker selalu memakai password.
- Deploy key CI hanya dipakai untuk server MR Pilot. Cabut dengan menghapus barisnya di `~/.ssh/authorized_keys` server.
- Mode `llm` mengirim diff kode ke provider AI. Pastikan sesuai kebijakan perusahaan (atau pakai LLM internal).
- Dashboard hanya bisa diakses dari PC itu sendiri, kecuali Anda membukanya ke jaringan dengan password.
- Semua aksi tercatat di `logs/mr-pilot.log`. Status MR tersimpan di `mr_pilot.db` (hapus file ini untuk reset).
- Token (Telegram, GitLab, API key AI, URL flow Teams) **disensor otomatis** dari log, dashboard, dan pesan error.
- Tombol **Keluar** (ikon pintu, kanan atas dashboard) mencabut sesi di server, bukan sekadar menghapus cookie.
- Login dashboard dikunci 15 menit setelah 10 kali salah dari alamat yang sama. Header CSP/anti-iframe aktif.

## 6a. Ketahanan (production)

- **Tidak ada kartu MR yang hilang**: kalau Telegram sedang gangguan, MR ditandai `notify_failed` dan dikirim ulang otomatis.
- **Merge aman**: tap dua kali, commit baru sesaat sebelum merge (SHA dicek), conflict, Draft, pipeline belum selesai, dan koneksi putus saat merge (status asli dicek ulang ke GitLab) semuanya ditangani.
- **Berhenti dengan aman**: `docker stop` / Ctrl+C menunggu merge yang sedang berjalan selesai (`stop_grace_period: 75s`). Kalau proses mati paksa di tengah merge, saat menyala lagi status MR dicocokkan ke GitLab.
- **Gangguan jaringan**: GitLab GET di-retry otomatis (bukan merge/komentar, agar tidak dobel); Telegram 429/5xx di-retry, gangguan panjang memakai backoff 5–60 detik dan hanya dicatat sekali.
- **Dua instance memakai bot yang sama** (mis. PC dan server): diberi peringatan jelas sekali, tidak spam.
- **AI**: jawaban AI yang rusak/bukan JSON dianggap gagal dan otomatis pindah ke provider berikutnya.
- **Health**: `python -m mr_pilot health` (dipakai Docker HEALTHCHECK) mengecek loop utama masih hidup; `GET /healthz` mengembalikan 503 sampai bot benar-benar berjalan.
- Data lama dibersihkan otomatis setiap hari (aktivitas & log AI 90 hari, pelanggaran standar 365 hari).
- Debug proses yang macet: `docker compose kill -s USR1 mr-pilot` menulis stack semua thread ke log.

## 7. Troubleshooting

| Gejala | Solusi |
|---|---|
| `CERTIFICATE_VERIFY_FAILED` / "Sertifikat SSL ... tidak dipercaya" | GitLab kantor memakai CA internal, SSL inspection, atau intermediate tidak lengkap. Jalankan **`setup.bat trust-cert`** (Windows mengambil rantai sertifikat yang ia percaya dan menyimpannya ke `data\certs`, lalu MR Pilot di-restart). Di Linux: `./setup.sh trust-cert`. Bisa juga taruh file CA dari tim IT (`.crt`/`.cer`/`.pem`) di `data/certs/`. Darurat saja: `GITLAB_VERIFY_SSL=false` di `data/.env` |
| Telegram timeout | Jaringan kantor memblokir Telegram, isi `telegram.proxy` |
| Selalu "menunggu komentar bot" | Cek `review.bot.usernames` / `marker` sesuai akun bot di MR |
| Teams `gagal: HTTP 4xx` | URL flow salah/kedaluwarsa, atau flow nonaktif. Cek *Run history* di Power Automate |
| Merge gagal 405/406 | MR belum mergeable (approval wajib, conflict, discussion belum resolved) |
| Warning tidak muncul di baris commit | Baris tersebut bukan bagian diff commit itu. Ringkasan tetap ada di MR |
| Login dashboard selalu "password salah" | Password diambil dari `DASHBOARD_PASSWORD` di **`data/.env`** (bukan `.env` di folder utama) dan dibaca **saat MR Pilot dinyalakan**. Setelah mengubah, jalankan `setup.bat restart`. Cek password aktif dengan `setup.bat password`. Hapus juga password lama yang tersimpan otomatis di browser. |
| `setup.bat`: "Docker belum berjalan" | Buka Docker Desktop, tunggu status *running*, ulangi |
| Linux: `permission denied ... docker.sock` | Logout/login (grup docker baru aktif), atau jalankan lagi; skrip memakai sudo otomatis |
| Claude Code: "belum login" | Jalankan `claude setup-token` di PC yang sudah login, tempel token di dashboard > AI |
| AI lokal tidak terhubung dari Docker | Pastikan Ollama listen di `0.0.0.0` (`OLLAMA_HOST=0.0.0.0`) atau pakai `--with-ollama` |
| Deploy CI gagal "config.yaml belum ada" | Jalankan `setup.bat ci` lagi dan pilih salin config ke server |
| Dashboard tidak bisa dibuka dari HP | `dashboard.host: 0.0.0.0`, isi password, dan izinkan port 8787 di Windows Firewall |

| Log: "Bot Telegram yang sama sedang dipakai MR Pilot lain (409)" | MR Pilot jalan di dua tempat dengan bot yang sama (PC dan server). Matikan salah satu |
| Log: "Dashboard tidak bisa memakai port 8787" | Port dipakai aplikasi lain. Ganti `MRP_PORT` (Docker) / `DASHBOARD_PORT` |

### Menguji

```bash
pip install -r requirements.txt pytest
python -m pytest -q --ignore=tests/e2e     # unit test (detik)
python -m pytest -q tests/e2e              # end-to-end ±3 menit: proses MR Pilot asli melawan
                                           # GitLab/Telegram/AI/Teams palsu (26 skenario)
E2E_MODE=docker E2E_IMAGE=mr-pilot:local python -m pytest -q tests/e2e   # sama, terhadap image Docker
```

Skenario end-to-end mencakup: kartu MR + fallback AI, tidak ada duplikat, merge menunggu pipeline, tap ganda, konfirmasi verdict/standar, tolak dengan/ tanpa komentar, commit baru mengganti kartu, SHA basi, conflict/Draft/HTTP 405, koneksi putus saat merge, user tak berizin, Telegram mati/429/HTML ditolak, kartu sangat panjang, GitLab 502, MR ditutup di luar, Teams gagal, lockout login, API dashboard + SSE + CSP, sensor rahasia di log, SIGTERM saat merge, restart & `kill -9` saat merge, konflik bot 409, config tidak valid, simpan standar/AI dari dashboard, dan tombol palsu.
