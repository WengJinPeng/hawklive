#!/usr/bin/env bash
set -euo pipefail

project_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
compose_file="$project_directory/docker-compose.cloud.yml"
action="${1:-status}"

cd "$project_directory"

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and fill in the production values." >&2
  exit 1
fi

case "$action" in
  start)
    mkdir -p backups/daily backups/monthly
    docker compose -f "$compose_file" up -d --build
    ;;
  stop)
    docker compose -f "$compose_file" stop
    ;;
  status)
    docker compose -f "$compose_file" ps
    docker compose -f "$compose_file" exec -T ingest-api python -c \
      "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5).read().decode())"
    ;;
  backup)
    docker compose -f "$compose_file" exec -T backup-runner python cloud_backup.py \
      --daily-dir /app/backups/daily --monthly-dir /app/monthly-backups
    ;;
  logs)
    docker compose -f "$compose_file" logs --tail 100
    ;;
  *)
    echo "Usage: bash cloud-ops.sh {start|stop|status|backup|logs}" >&2
    exit 2
    ;;
esac
