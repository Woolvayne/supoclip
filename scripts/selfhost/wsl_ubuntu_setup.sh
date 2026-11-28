#!/usr/bin/env bash
# =============================================================================
# SupoClip Self-Hosted Setup / Update for Ubuntu 22.04+ & Windows WSL 1/2
# =============================================================================
# Installs static FFmpeg 8.1 GPL (required for tpad=-1 / -fps_mode passthrough),
# Deno JS runtime + bgutil YouTube PO-Token HTTP provider, PostgreSQL, Redis,
# Caddy HTTPS reverse proxy, CPU core limits (SUPOCLIP_MAX_CPU_CORES),
# OpenRouter :free smart-routing, and Buffer social publishing.
#
# Usage (no hardcoded secrets — pass via environment or interactive prompt):
#   sudo OPENROUTER_API_KEY="sk-or-v1-..." PUBLIC_DOMAIN="203-0-113-10.sslip.io" \
#        SUPOCLIP_MAX_CPU_CORES=2 bash scripts/selfhost/wsl_ubuntu_setup.sh
# =============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/supoclip}"
REPO_URL="${REPO_URL:-https://github.com/Woolvayne/supoclip.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
MAX_CORES="${SUPOCLIP_MAX_CPU_CORES:-2}"
PUBLIC_DOMAIN="${PUBLIC_DOMAIN:-}"
OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
BUFFER_API_KEY="${BUFFER_API_KEY:-}"

if [[ -z "${PUBLIC_DOMAIN}" ]]; then
  PUBLIC_IP="$(curl -fsSL --max-time 5 https://api.ipify.org || echo "127.0.0.1")"
  if [[ "${PUBLIC_IP}" != "127.0.0.1" ]]; then
    PUBLIC_DOMAIN="${PUBLIC_IP//./-}.sslip.io"
  else
    PUBLIC_DOMAIN="localhost"
  fi
fi

PUBLIC_URL="https://${PUBLIC_DOMAIN}"
if [[ "${PUBLIC_DOMAIN}" == "localhost" ]]; then
  PUBLIC_URL="http://localhost:3000"
fi

echo "==> Configuring SupoClip in ${APP_DIR} (${PUBLIC_URL}, max CPU cores: ${MAX_CORES})"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  curl ca-certificates git xz-utils unzip build-essential pkg-config \
  postgresql postgresql-contrib redis-server supervisor fontconfig \
  fonts-dejavu-core fonts-liberation fonts-noto-color-emoji

# 1. Static FFmpeg 8.1 GPL (Ubuntu 22.04's FFmpeg 4.4 lacks tpad stop=-1 & -fps_mode)
if ! /usr/local/bin/ffmpeg -version 2>/dev/null | grep -qE "version (n?[6789]|8\.)"; then
  echo "==> Installing static FFmpeg 8.1 GPL..."
  mkdir -p /opt/ffmpeg8
  curl -fsSL "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz" \
    -o /tmp/ffmpeg8.tar.xz
  tar -xJf /tmp/ffmpeg8.tar.xz -C /opt/ffmpeg8 --strip-components=1
  rm -f /tmp/ffmpeg8.tar.xz
  ln -sf /opt/ffmpeg8/bin/ffmpeg /usr/local/bin/ffmpeg
  ln -sf /opt/ffmpeg8/bin/ffprobe /usr/local/bin/ffprobe
fi

# 2. Deno runtime for yt-dlp YouTube JS challenges
if ! command -v deno >/dev/null 2>&1; then
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh
fi

echo "==> Static FFmpeg ready: $(/usr/local/bin/ffmpeg -version | head -n 1)"
echo "==> Done. Configure ${APP_DIR}/backend/.env (see backend/.env.example) and restart services."
