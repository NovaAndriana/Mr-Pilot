#!/usr/bin/env bash
# Dijalankan di SERVER oleh pipeline CI (GitHub Actions / GitLab CI) lewat SSH.
# Env : IMAGE (wajib), REGISTRY, REGISTRY_USER (opsional, untuk image private)
# Stdin: password registry (opsional) -> tidak pernah muncul di argumen proses / `ps`.
# Kalau image baru tidak sehat dalam 3 menit, otomatis kembali ke image sebelumnya.
set -euo pipefail
cd "$(dirname "$0")"

: "${IMAGE:?IMAGE wajib diisi}"
DC="docker compose"
command -v docker >/dev/null || { echo "Docker belum terpasang di server. Jalankan setup-ci."; exit 1; }
$DC version >/dev/null 2>&1 || DC="docker-compose"

if [ ! -f data/config.yaml ] || [ ! -f data/.env ]; then
  echo "data/config.yaml atau data/.env belum ada di $(pwd)."
  echo "Jalankan 'setup.sh ci' dari PC Anda (menyalin config), atau 'setup.sh' langsung di server."
  exit 1
fi

REGISTRY_PASSWORD=""
if [ ! -t 0 ]; then IFS= read -r REGISTRY_PASSWORD || true; fi

touch .env
get_kv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
set_kv() {
  local tmp; tmp=$(mktemp)
  grep -v -E "^$1=" .env > "$tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$tmp"
  cat "$tmp" > .env && rm -f "$tmp"
}

PREV_IMAGE=$(get_kv MRP_IMAGE)
set_kv MRP_IMAGE "$IMAGE"
[ -n "$(get_kv MRP_UID)" ] || set_kv MRP_UID "$(id -u)"
[ -n "$(get_kv MRP_GID)" ] || set_kv MRP_GID "$(id -g)"
mkdir -p data/home

cleanup() { if [ -n "$REGISTRY_PASSWORD" ]; then docker logout "${REGISTRY:-ghcr.io}" >/dev/null 2>&1 || true; fi; }
trap cleanup EXIT

if [ -n "$REGISTRY_PASSWORD" ]; then
  printf '%s' "$REGISTRY_PASSWORD" | docker login "${REGISTRY:-ghcr.io}" -u "${REGISTRY_USER:-ci}" --password-stdin >/dev/null
fi

wait_healthy() {
  # Docker HEALTHCHECK = heartbeat loop utama MR Pilot (python -m mr_pilot health)
  local cid st i
  echo -n "==> menunggu sehat "
  for i in $(seq 1 90); do
    cid=$($DC ps -q mr-pilot 2>/dev/null || true)
    if [ -n "$cid" ]; then
      st=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo "?")
      case "$st" in
        healthy) echo " OK"; return 0 ;;
        unhealthy|exited|dead) echo " $st"; return 1 ;;
      esac
      if [ "$(docker inspect -f '{{.RestartCount}}' "$cid" 2>/dev/null || echo 0)" -ge 3 ]; then
        echo " restart berulang"; return 1
      fi
    fi
    echo -n "."; sleep 2
  done
  echo " timeout"; return 1
}

echo "==> pull $IMAGE"
$DC pull mr-pilot
echo "==> up"
$DC up -d --remove-orphans

if ! wait_healthy; then
  echo "==> image baru tidak sehat. Log terakhir:"
  $DC logs --tail 80 mr-pilot || true
  if [ -n "$PREV_IMAGE" ] && [ "$PREV_IMAGE" != "$IMAGE" ]; then
    echo "==> rollback ke $PREV_IMAGE"
    set_kv MRP_IMAGE "$PREV_IMAGE"
    $DC up -d --remove-orphans
    wait_healthy || echo "!! rollback juga belum sehat, cek server secara manual"
  fi
  exit 1
fi

docker image prune -f >/dev/null 2>&1 || true
echo "==> deploy selesai: $IMAGE"
