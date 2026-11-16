#!/usr/bin/env bash
# Daily PostgreSQL backup: compressed pg_dump, integrity check, local
# retention, optional off-box copy with rclone. Run by the systemd timer in
# deploy/systemd/, or by hand: deploy/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."
set -a
# shellcheck disable=SC1091
source ./.env
set +a

compose=(docker compose -f compose.prod.yaml)
backup_dir="${BACKUP_DIR:-/var/backups/job-board}"
keep_days="${BACKUP_KEEP_DAYS:-14}"
remote="${RCLONE_REMOTE:-}"
remote_keep_days="${REMOTE_KEEP_DAYS:-7}"

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*"; }

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
file="${backup_dir}/job_board-${stamp}.dump"
partial="${file}.partial"
trap 'rm -f "$partial"' EXIT

log "dumping ${POSTGRES_DB} to ${file}"
"${compose[@]}" exec -T postgres \
  pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6 \
  > "$partial"

# A dump that pg_restore cannot list is useless: fail loudly, keep old ones.
"${compose[@]}" exec -T postgres pg_restore --list < "$partial" > /dev/null
mv "$partial" "$file"
chmod 600 "$file"
log "ok ($(du -h "$file" | cut -f1))"

find "$backup_dir" -maxdepth 1 -name 'job_board-*.dump' -mtime "+${keep_days}" -print -delete

if [[ -n "$remote" ]]; then
  log "copying to ${remote}"
  rclone copy "$file" "$remote" --no-traverse
  rclone delete "$remote" --min-age "${remote_keep_days}d" --include 'job_board-*.dump'
  log "remote copy ok"
fi
