#!/usr/bin/env bash
# Restore a backup made by deploy/backup.sh into the running postgres
# container. Stops the app services first and starts them again after.
#   deploy/restore.sh /var/backups/job-board/job_board-20261116T221500Z.dump
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source ./.env
set +a

dump="${1:?Usage: $0 <path to .dump file>}"
[[ -f "$dump" ]] || { echo "No such file: $dump" >&2; exit 1; }

compose=(docker compose -f compose.prod.yaml)

read -r -p "This REPLACES all data in ${POSTGRES_DB} with ${dump}. Type 'restore' to continue: " answer
[[ "$answer" == "restore" ]] || { echo "Aborted."; exit 1; }

"${compose[@]}" stop api worker beat
"${compose[@]}" exec -T postgres pg_restore \
  -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --exit-on-error \
  < "$dump"
"${compose[@]}" up -d migrate api worker beat
echo "Restore finished."
