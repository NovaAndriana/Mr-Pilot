#!/usr/bin/env bash
# Dijalankan di SERVER oleh pipeline CI (GitHub Actions / GitLab CI) lewat SSH.
# Env: IMAGE (wajib), REGISTRY, REGISTRY_USER, REGISTRY_PASSWORD (opsional, untuk image private)
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

touch .env
set_kv() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
set_kv MRP_IMAGE "$IMAGE"
grep -q '^MRP_UID=' .env || set_kv MRP_UID "$(id -u)"
grep -q '^MRP_GID=' .env || set_kv MRP_GID "$(id -g)"
mkdir -p data/home

if [ -n "${REGISTRY_PASSWORD:-}" ]; then
  echo "$REGISTRY_PASSWORD" | docker login "${REGISTRY:-ghcr.io}" -u "${REGISTRY_USER:-ci}" --password-stdin >/dev/null
fi

echo "==> pull $IMAGE"
$DC pull mr-pilot
echo "==> up"
$DC up -d --remove-orphans

PORT=$(grep -E '^MRP_PORT=' .env | cut -d= -f2); PORT=${PORT:-8787}
echo -n "==> health "
for i in $(seq 1 30); do
  if curl -fs "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then echo "OK"; break; fi
  if [ "$i" = 30 ]; then echo "GAGAL"; $DC logs --tail 50 mr-pilot; exit 1; fi
  echo -n "."; sleep 2
done

[ -n "${REGISTRY_PASSWORD:-}" ] && docker logout "${REGISTRY:-ghcr.io}" >/dev/null 2>&1 || true
docker image prune -f >/dev/null 2>&1 || true
echo "==> deploy selesai: $IMAGE"
