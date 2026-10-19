#!/bin/sh
# Install and start Rootvanta with Docker: ./install.sh
# Creates .env with new random secrets the first time, then starts everything.
set -eu
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Docker is needed: https://docs.docker.com/get-docker/"; exit 1; }
secret() { python3 -c "import secrets; print(secrets.token_urlsafe(48))" 2>/dev/null || openssl rand -base64 48 | tr -d '/+=\n'; }
if [ ! -f .env ]; then
  cat > .env <<ENV
# Made by install.sh. Keep this file private, and back it up: without
# INCIDENT_AGENT_SECRET_KEY the saved monitoring tokens cannot be read.
INCIDENT_AGENT_JWT_SECRET=$(secret)
INCIDENT_AGENT_SECRET_KEY=$(secret)
POSTGRES_PASSWORD=$(secret)
APP_DB_PASSWORD=$(secret)
# Set to 1 when several companies share this server (blocks private network addresses).
INCIDENT_AGENT_BLOCK_PRIVATE_URLS=0
WORKERS=2
ENV
  echo "Created .env with new secrets."
fi
docker compose up -d --build
echo
echo "Rootvanta is starting. Open http://localhost:3001 and create your company."
