#!/usr/bin/env bash
# MR Pilot — instal & kelola dengan Docker (Linux server, macOS, WSL, Git Bash).
#
#   ./setup.sh                 instal: Docker (jika belum ada) -> build -> wizard -> jalan
#   ./setup.sh --server        sama, dashboard bisa dibuka dari jaringan (bind 0.0.0.0)
#   ./setup.sh ci              pasang CI/CD (GitHub/GitLab) + deploy otomatis ke server
#   ./setup.sh update | start | stop | restart | status | logs | doctor | config | shell | demo
#   ./setup.sh password [--reset]   lihat / buat ulang password dashboard
#   ./setup.sh reset [--all] [-y]   hapus riwayat MR, aktivitas & log (token/.env, config, standar tetap)
#   ./setup.sh trust-cert [--url=https://...]  percayai sertifikat SSL GitLab (CERTIFICATE_VERIFY_FAILED)
#
# Opsi: --server  --port N  --with-claude-code  --with-ollama[=model]  --non-interactive  -y
set -euo pipefail
cd "$(dirname "$0")"

CMD=install; PW_RESET=0; ALL=0; BIND=""; PORT=""; CLAUDE=""; OLLAMA=""; OLLAMA_MODEL=""; NONINT=0; YES=0
for arg in "$@"; do
  case "$arg" in
    install|update|ci|start|stop|restart|status|logs|doctor|config|shell|demo|password|reset|trust-cert) CMD=$arg ;;
    --url=*) TRUST_URL=${arg#*=} ;;
    --all) ALL=1 ;;
    --reset) PW_RESET=1 ;;
    --server) BIND=0.0.0.0 ;;
    --local) BIND=127.0.0.1 ;;
    --port=*) PORT=${arg#*=} ;;
    --with-claude-code) CLAUDE=true ;;
    --with-ollama) OLLAMA=true ;;
    --with-ollama=*) OLLAMA=true; OLLAMA_MODEL=${arg#*=} ;;
    --non-interactive) NONINT=1; YES=1 ;;
    -y|--yes) YES=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "Opsi tidak dikenal: $arg"; exit 2 ;;
  esac
done

b=$(tput bold 2>/dev/null || true); n=$(tput sgr0 2>/dev/null || true)
say() { echo "${b}==>${n} $*"; }
ask_yn() { # ask_yn "pertanyaan" default(y/n)
  [ "$YES" = 1 ] && { [ "$2" = y ]; return; }
  read -r -p "    $1 [$( [ "$2" = y ] && echo Y/n || echo y/N )]: " a || true
  a=${a:-$2}; [[ "$a" =~ ^[YyJj] ]]
}
kv() { { grep -E "^$1=" "${2:-.env}" 2>/dev/null || true; } | tail -1 | cut -d= -f2- | tr -d '"'; }
set_kv() { touch .env; if grep -q "^$1=" .env; then sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak; else echo "$1=$2" >> .env; fi; }

trust_cert() {
  # Simpan penerbit sertifikat server ke data/certs (CA kantor / intermediate yang tidak dikirim server)
  local url="${1:-$(kv GITLAB_URL data/.env)}"
  [ -n "$url" ] || { echo "GITLAB_URL belum diisi (data/.env)."; exit 1; }
  local hp="${url#*://}"; hp="${hp%%/*}"; local host="${hp%%:*}" port=443
  [ "$hp" != "$host" ] && port="${hp#*:}"
  command -v openssl >/dev/null || { echo "Butuh openssl."; exit 1; }
  say "Mengambil sertifikat $host:$port"
  mkdir -p data/certs
  local tmp; tmp=$(mktemp -d)
  openssl s_client -connect "$host:$port" -servername "$host" -showcerts </dev/null 2>/dev/null \
    | awk -v d="$tmp" '/BEGIN CERTIFICATE/{n++; p=1} p{print > (d "/c" n ".pem")} /END CERTIFICATE/{p=0}'
  local out="data/certs/$host-chain.pem"; : > "$out.tmp"
  local f
  for f in $(ls "$tmp"/c*.pem 2>/dev/null | sort -V | tail -n +2); do cat "$f" >> "$out.tmp"; done
  # issuer of the last cert from the system trust store (company CA installed on this server)
  local last; last=$(ls "$tmp"/c*.pem 2>/dev/null | sort -V | tail -1)
  if [ -n "$last" ]; then
    local h; h=$(openssl x509 -in "$last" -noout -issuer_hash 2>/dev/null || true)
    for f in /etc/ssl/certs/"$h".*; do [ -f "$f" ] && cat "$f" >> "$out.tmp"; done
  fi
  rm -rf "$tmp"
  if ! grep -q "BEGIN CERTIFICATE" "$out.tmp"; then
    rm -f "$out.tmp"
    echo "Penerbit sertifikat $host tidak ditemukan. Minta file CA ke tim IT, taruh di data/certs/, lalu restart."
    echo "Darurat: GITLAB_VERIFY_SSL=false di data/.env."; exit 1
  fi
  mv "$out.tmp" "$out"
  say "Disimpan: $out ($(grep -c 'BEGIN CERTIFICATE' "$out") sertifikat)"
}

# ------------------------------------------------------------------ docker
SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null && SUDO="sudo"
DOCKER="docker"
ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    case "$(uname -s)" in
      Linux)
        say "Docker belum terpasang."
        ask_yn "Instal Docker sekarang (get.docker.com)?" y || { echo "Batal."; exit 1; }
        curl -fsSL https://get.docker.com | $SUDO sh
        [ -n "$SUDO" ] && $SUDO usermod -aG docker "$USER" || true
        ;;
      Darwin) echo "Instal Docker Desktop dulu: brew install --cask docker  (lalu buka Docker.app)"; exit 1 ;;
      *) echo "Instal Docker Desktop dulu: https://www.docker.com/products/docker-desktop/  (Windows: pakai setup.bat)"; exit 1 ;;
    esac
  fi
  if ! docker info >/dev/null 2>&1; then
    if [ -n "$SUDO" ] && $SUDO docker info >/dev/null 2>&1; then
      DOCKER="$SUDO docker"   # grup docker belum aktif di sesi ini
    elif [ "$(uname -s)" = Linux ]; then
      $SUDO systemctl enable --now docker >/dev/null 2>&1 || true; sleep 2
      docker info >/dev/null 2>&1 || DOCKER="$SUDO docker"
    else
      echo "Docker belum berjalan. Buka Docker Desktop lalu ulangi."; exit 1
    fi
  fi
  $DOCKER compose version >/dev/null 2>&1 || { echo "Plugin 'docker compose' tidak ada. Update Docker."; exit 1; }
}
dc() { $DOCKER compose "$@"; }

wait_healthy() {
  local port; port=$(kv MRP_PORT); port=${port:-8787}
  printf "    menunggu MR Pilot siap "
  for _ in $(seq 1 40); do
    if curl -fs "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then echo "OK"; return 0; fi
    printf "."; sleep 2
  done
  echo; say "Belum merespons. Cek log: ./setup.sh logs"; return 1
}

summary() {
  local port bind host pw
  port=$(kv MRP_PORT); port=${port:-8787}; bind=$(kv MRP_BIND)
  host=127.0.0.1
  if [ "$bind" = 0.0.0.0 ]; then host=$(hostname -I 2>/dev/null | awk '{print $1}'); host=${host:-$(hostname)}; fi
  pw=$(kv DASHBOARD_PASSWORD data/.env)
  echo
  say "${b}MR Pilot berjalan${n}"
  echo "    Dashboard : http://${host}:${port}"
  echo "    Password  : ${pw:-'(lihat data/.env)'}"
  echo "    Perintah  : ./setup.sh logs | status | doctor | update | stop | ci"
}

# ---------------------------------------------------------------- commands
case "$CMD" in
  install)
    ensure_docker
    say "Konfigurasi Docker"
    if [ ! -f .env ] || [ -n "$BIND$PORT$CLAUDE$OLLAMA" ]; then
      if [ -z "$BIND" ]; then
        if ask_yn "Dashboard bisa dibuka dari komputer lain di jaringan (mode server)?" n; then BIND=0.0.0.0; else BIND=127.0.0.1; fi
      fi
      if [ -z "$CLAUDE" ]; then ask_yn "Pasang CLI Claude Code di container (pakai langganan Claude)?" n && CLAUDE=true || CLAUDE=false; fi
      if [ -z "$OLLAMA" ]; then ask_yn "Jalankan AI lokal Ollama di Docker juga?" n && OLLAMA=true || OLLAMA=false; fi
      set_kv MRP_BIND "$BIND"
      PORT=${PORT:-$(kv MRP_PORT)}; set_kv MRP_PORT "${PORT:-8787}"
      set_kv INSTALL_CLAUDE_CODE "$CLAUDE"
      set_kv MRP_UID "$(id -u)"; set_kv MRP_GID "$(id -g)"
      if [ "$OLLAMA" = true ]; then set_kv COMPOSE_PROFILES local-ai; set_kv MRP_OLLAMA_BUNDLED 1
      else set_kv COMPOSE_PROFILES ""; set_kv MRP_OLLAMA_BUNDLED 0; fi
      [ -z "$(kv TZ)" ] && set_kv TZ "${TZ:-Asia/Jakarta}"
    fi
    mkdir -p data/home
    say "Build image (beberapa menit pertama kali)"
    dc build
    say "Wizard konfigurasi"
    if [ "$NONINT" = 1 ]; then dc run --rm -T mr-pilot setup --non-interactive; else dc run --rm mr-pilot setup; fi
    say "Menjalankan"
    dc up -d
    if [ "$(kv COMPOSE_PROFILES)" = local-ai ]; then
      m=${OLLAMA_MODEL:-$(kv LOCAL_AI_MODEL data/.env)}; m=${m:-qwen2.5-coder:14b}
      say "Mengunduh model Ollama $m (sekali saja, bisa lama)"
      dc exec -T ollama ollama pull "$m" || echo "    Gagal pull model. Coba lagi: docker compose exec ollama ollama pull $m"
    fi
    wait_healthy || true
    summary
    ;;
  update)
    ensure_docker
    if [ -d .git ]; then say "git pull"; git pull --ff-only || true; fi
    say "Build & restart"
    if [ -n "$(kv MRP_IMAGE)" ] && [ "$(kv MRP_IMAGE)" != "mr-pilot:local" ]; then dc pull mr-pilot; else dc build; fi
    dc up -d --remove-orphans
    wait_healthy || true; summary ;;
  ci)
    ensure_docker
    [ -f data/config.yaml ] || { echo "Jalankan ./setup.sh dulu (config belum ada)."; exit 1; }
    dc build >/dev/null
    dc run --rm -v "$PWD:/src" -e MRP_SRC=/src \
      -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*' \
      mr-pilot setup-ci ;;
  start) ensure_docker; dc up -d; wait_healthy || true; summary ;;
  stop) ensure_docker; dc down ;;
  restart) ensure_docker; dc restart mr-pilot; wait_healthy || true ;;
  trust-cert)
    trust_cert "${TRUST_URL:-}"
    if command -v docker >/dev/null && [ -n "$($DOCKER compose ps -q mr-pilot 2>/dev/null)" ]; then
      dc restart mr-pilot && wait_healthy || true
    fi ;;
  status) ensure_docker; dc ps ;;
  reset)
    ensure_docker
    dc stop mr-pilot >/dev/null 2>&1 || true   # database must not be in use
    rargs=(reset); [ "$YES" = 1 ] && rargs+=(--yes); [ "$ALL" = 1 ] && rargs+=(--all)
    if dc run --rm mr-pilot "${rargs[@]}"; then
      dc up -d --force-recreate mr-pilot && wait_healthy || true   # new container = Docker log history cleared too
    else
      dc start mr-pilot >/dev/null 2>&1 || true
    fi ;;
  logs) ensure_docker; dc logs -f --tail 100 mr-pilot ;;
  doctor) ensure_docker; dc run --rm -T mr-pilot doctor ;;
  password)
    ensure_docker
    if [ "$PW_RESET" = 1 ]; then dc run --rm -T mr-pilot password --reset && dc restart mr-pilot && wait_healthy || true
    else dc run --rm -T mr-pilot password; fi ;;
  config) ensure_docker
          # stop the running bot first: the wizard polls the same Telegram bot (409) and rewrites .env
          dc stop mr-pilot >/dev/null 2>&1 || true
          dc run --rm mr-pilot setup
          dc up -d mr-pilot && wait_healthy ;;
  shell) ensure_docker; dc exec mr-pilot bash ;;
  demo) ensure_docker; dc build >/dev/null; echo "Dashboard demo: http://127.0.0.1:${PORT:-8788}  (password: demo, Ctrl+C untuk berhenti)"
        dc run --rm -p "127.0.0.1:${PORT:-8788}:8787" mr-pilot demo ;;
esac
