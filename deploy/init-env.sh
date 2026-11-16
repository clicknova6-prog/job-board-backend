#!/usr/bin/env bash
# Create ./.env from deploy/env.production.example with random secrets.
#   deploy/init-env.sh --domain api.example.com --email you@example.com
# Refuses to overwrite an existing .env.
set -euo pipefail

cd "$(dirname "$0")/.."

domain=""
email=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) domain="$2"; shift 2 ;;
    --email) email="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$domain" || -z "$email" ]]; then
  echo "Usage: $0 --domain api.example.com --email you@example.com" >&2
  exit 2
fi
if [[ -e .env ]]; then
  echo ".env already exists; refusing to overwrite it." >&2
  exit 1
fi

secret() { openssl rand -hex "$1"; }
db_password="$(secret 24)"
jwt_secret="$(secret 48)"

umask 077
sed \
  -e "s|^POSTGRES_PASSWORD=__GENERATE__|POSTGRES_PASSWORD=${db_password}|" \
  -e "s|__POSTGRES_PASSWORD__|${db_password}|" \
  -e "s|^JWT_SECRET=__GENERATE__|JWT_SECRET=${jwt_secret}|" \
  -e "s|^API_DOMAIN=.*|API_DOMAIN=${domain}|" \
  -e "s|^ACME_EMAIL=.*|ACME_EMAIL=${email}|" \
  -e "s|https://api.example.com/auth/google/callback|https://${domain}/auth/google/callback|" \
  deploy/env.production.example > .env

echo "Wrote .env (mode 600). Still to fill in: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, CORS_ALLOWED_ORIGINS."
