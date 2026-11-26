#!/usr/bin/env bash
# =============================================================================
# SupoClip Universal Self-Heal, Recovery & Live Updater (WSL 1/2 & Ubuntu 22.04+)
# =============================================================================
# Checks ALL layers without double-installing existing components:
#   1. Secrets & SSLIP.IO configuration (Port 1978 + 443)
#   2. System packages (PostgreSQL, Redis, Python3, Node.js 20+, Deno, FFmpeg 8.1, bgutil)
#   3. Git repository sync (newest commit across main & arena/* branches)
#   4. PostgreSQL role/database & schema migrations
#   5. Python backend .venv health & packages
#   6. Next.js standalone frontend & Prisma engine (hash-cached, zero redundant builds)
#   7. Supervisor with 2-CPU-core cap, OpenRouter :free Smart-Router & Buffer
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export PNPM_HOME="${PNPM_HOME:-/root/.local/share/pnpm}"
export PATH="/usr/local/bin:/opt/ffmpeg8/bin:${PNPM_HOME}:/pnpm:/root/.local/bin:/root/.deno/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

APP_DIR="${APP_DIR:-/opt/supoclip}"
REPO_URL="${REPO_URL:-https://github.com/Woolvayne/supoclip.git}"
REPO_BRANCH="${REPO_BRANCH:-auto}"
MAX_CORES="${SUPOCLIP_MAX_CPU_CORES:-2}"
FORCE_REBUILD="${FORCE_REBUILD:-0}"
FORCE_FRONTEND_BUILD="${FORCE_FRONTEND_BUILD:-${FORCE_REBUILD}}"

mkdir -p "${APP_DIR}" /var/lib/supoclip/uploads /var/lib/supoclip/clips /var/log/supoclip

# -----------------------------------------------------------------------------
# [Bereich A] Secrets & SSLIP.IO Konfiguration (Port 1978 + 443)
# -----------------------------------------------------------------------------
echo "==> [A/G] Pruefe Konfiguration, Secrets & sslip.io-Adresse (Port 1978)..."
SECRETS_FILE="${APP_DIR}/.secrets.env"
SECRETS_BACKUP="/root/.supoclip_secrets_backup.env"

if [[ -f "${SECRETS_FILE}" ]]; then
  cp -fp "${SECRETS_FILE}" "${SECRETS_BACKUP}"
  chmod 600 "${SECRETS_BACKUP}"
  # shellcheck disable=SC1090
  source "${SECRETS_FILE}"
elif [[ -f "${SECRETS_BACKUP}" ]]; then
  cp -fp "${SECRETS_BACKUP}" "${SECRETS_FILE}"
  chmod 600 "${SECRETS_FILE}"
  # shellcheck disable=SC1090
  source "${SECRETS_FILE}"
fi

# Rescue existing keys from run_supervisor.sh or backend/.env if .secrets.env was missing
for candidate_env in "${APP_DIR}/backend/.env" "${APP_DIR}/run_supervisor.sh"; do
  if [[ -f "${candidate_env}" ]]; then
    if [[ -z "${OPENROUTER_KEY:-}" ]]; then
      RESCUED_OR="$(grep -Eo 'OPENROUTER_API_KEY=["'"'"']?sk-or-[A-Za-z0-9_-]+' "${candidate_env}" 2>/dev/null | head -n1 | cut -d= -f2 | tr -d '"'"'" || true)"
      [[ -n "${RESCUED_OR}" ]] && OPENROUTER_KEY="${RESCUED_OR}"
    fi
    if [[ -z "${PG_PASS:-}" ]]; then
      RESCUED_PG="$(grep -Eo 'postgresql(\+asyncpg)?://supoclip:[^@]+@127\.0\.0\.1' "${candidate_env}" 2>/dev/null | head -n1 | sed -E 's|.*://supoclip:([^@]+)@.*|\1|' || true)"
      [[ -n "${RESCUED_PG}" && "${RESCUED_PG}" != '${PG_PASS}' ]] && PG_PASS="${RESCUED_PG}"
    fi
  fi
done

DETECTED_IP="$(curl -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || curl -fsSL --max-time 5 https://ifconfig.me 2>/dev/null || echo "${PUBLIC_IP:-127.0.0.1}")"
DETECTED_IP="$(echo "${DETECTED_IP}" | tr -d '[:space:]')"
if [[ "${DETECTED_IP}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  PUBLIC_IP="${DETECTED_IP}"
else
  PUBLIC_IP="${PUBLIC_IP:-127.0.0.1}"
fi

HTTPS_PORT="${HTTPS_PORT:-1978}"
SSLIP_HOST="${PUBLIC_IP//./-}.sslip.io"
PRIMARY_URL="https://${SSLIP_HOST}:${HTTPS_PORT}"
STD_HTTPS_URL="https://${SSLIP_HOST}"

rand_hex() {
  openssl rand -hex 24 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(24))"
}

PG_PASS="${PG_PASS:-$(rand_hex)}"
AUTH_SEC="${AUTH_SEC:-$(rand_hex)}"
BACK_SEC="${BACK_SEC:-$(rand_hex)}"
ENC_SEC="${ENC_SEC:-$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())' 2>/dev/null || rand_hex)}"
OPENROUTER_KEY="${OPENROUTER_KEY:-${OPENROUTER_API_KEY:-}}"
FX_MODE="${FX_MODE:-off}"

cat <<EOF > "${SECRETS_FILE}"
PUBLIC_IP="${PUBLIC_IP}"
HTTPS_PORT="${HTTPS_PORT}"
SSLIP_HOST="${SSLIP_HOST}"
PG_PASS="${PG_PASS}"
AUTH_SEC="${AUTH_SEC}"
BACK_SEC="${BACK_SEC}"
ENC_SEC="${ENC_SEC}"
OPENROUTER_KEY="${OPENROUTER_KEY}"
FX_MODE="${FX_MODE}"
EOF
chmod 600 "${SECRETS_FILE}"
cp -fp "${SECRETS_FILE}" "${SECRETS_BACKUP}"
chmod 600 "${SECRETS_BACKUP}"
echo "    [OK] Adresse konfiguriert: ${PRIMARY_URL} (und ${STD_HTTPS_URL})"

# -----------------------------------------------------------------------------
# [Bereich B] Linux-Systempakete, FFmpeg 8.1, Node.js, Deno & bgutil pruefen
# -----------------------------------------------------------------------------
echo "==> [B/G] Pruefe Linux-Systempakete & Media-Tools (installiere NUR Fehlendes)..."
MISSING_PKGS=()
command -v curl >/dev/null 2>&1 || MISSING_PKGS+=(curl ca-certificates)
command -v git >/dev/null 2>&1 || MISSING_PKGS+=(git)
command -v xz >/dev/null 2>&1 || MISSING_PKGS+=(xz-utils)
command -v unzip >/dev/null 2>&1 || MISSING_PKGS+=(unzip)
command -v psql >/dev/null 2>&1 || MISSING_PKGS+=(postgresql postgresql-contrib)
command -v redis-server >/dev/null 2>&1 || MISSING_PKGS+=(redis-server)
command -v python3 >/dev/null 2>&1 || MISSING_PKGS+=(python3 python3-venv python3-pip)
python3 -m venv --help >/dev/null 2>&1 || MISSING_PKGS+=(python3-venv)
command -v fc-list >/dev/null 2>&1 || MISSING_PKGS+=(fontconfig fonts-dejavu-core fonts-liberation fonts-noto-color-emoji)

if (( ${#MISSING_PKGS[@]} > 0 )); then
  echo "    [REPAIR] Installiere fehlende Systempakete: ${MISSING_PKGS[*]}"
  apt-get update -qq
  apt-get install -y -qq "${MISSING_PKGS[@]}"
else
  echo "    [OK] Alle Basis-Systempakete (PostgreSQL, Redis, Python3, Git, Fonts) vorhanden."
fi

# Node.js >= 20 pruefen
NEED_NODE=0
if ! command -v node >/dev/null 2>&1; then
  NEED_NODE=1
else
  NODE_MAJOR="$(node -v | sed -E 's/^v([0-9]+).*/\1/' || echo 0)"
  if (( NODE_MAJOR < 18 )); then
    NEED_NODE=1
  fi
fi
if [[ "${NEED_NODE}" == "1" ]]; then
  echo "    [REPAIR] Installiere Node.js 20 LTS..."
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y -qq nodejs
else
  echo "    [OK] Node.js vorhanden ($(node -v))."
fi

# Statisches FFmpeg 8.1 GPL pruefen
if [[ -f /usr/local/bin/ffmpeg ]] && head -c 2 /usr/local/bin/ffmpeg 2>/dev/null | grep -q '#!'; then
  rm -f /usr/local/bin/ffmpeg
fi
if ! /usr/local/bin/ffmpeg -version 2>/dev/null | grep -qE "version (n?[6789]|8\.)"; then
  echo "    [REPAIR] Installiere statisches FFmpeg 8.1 GPL..."
  mkdir -p /opt/ffmpeg8
  curl -fL --retry 3 \
    "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz" \
    -o /tmp/ffmpeg8.tar.xz
  tar -xJf /tmp/ffmpeg8.tar.xz -C /opt/ffmpeg8 --strip-components=1
  rm -f /tmp/ffmpeg8.tar.xz
  ln -sf /opt/ffmpeg8/bin/ffmpeg /usr/local/bin/ffmpeg
  ln -sf /opt/ffmpeg8/bin/ffprobe /usr/local/bin/ffprobe
else
  echo "    [OK] Statisches FFmpeg bereit ($(/usr/local/bin/ffmpeg -version | head -n 1 | awk '{print $1, $2, $3}'))."
fi

# Deno Runtime pruefen
if ! command -v deno >/dev/null 2>&1 && [[ ! -x /usr/local/bin/deno && ! -x /root/.deno/bin/deno ]]; then
  echo "    [REPAIR] Installiere Deno Runtime..."
  curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh >/dev/null 2>&1 || true
else
  echo "    [OK] Deno Runtime vorhanden."
fi

# bgutil PO-Token Server pruefen
if [[ ! -f /opt/bgutil/server/build/main.js ]]; then
  echo "    [REPAIR] Baue bgutil YouTube PO-Token Server..."
  rm -rf /opt/bgutil
  if git clone --depth 1 --branch 1.2.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil >/dev/null 2>&1; then
    (cd /opt/bgutil/server && npm install --no-audit --no-fund >/dev/null 2>&1 && npx tsc >/dev/null 2>&1) || true
  fi
else
  echo "    [OK] bgutil PO-Token Server vorhanden."
fi

# -----------------------------------------------------------------------------
# [Bereich C] Git-Repository pruefen & mit neuestem Stand synchronisieren
# -----------------------------------------------------------------------------
echo "==> [C/G] Pruefe SupoClip-Repository in ${APP_DIR}..."
git config --global --add safe.directory "${APP_DIR}" >/dev/null 2>&1 || true
cd "${APP_DIR}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "    [REPAIR] Initialisiere fehlendes Git-Repository in ${APP_DIR}..."
  git init -q
  git remote add origin "${REPO_URL}"
elif git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${REPO_URL}"
else
  git remote add origin "${REPO_URL}"
fi

PREV_HEAD="$(git rev-parse HEAD 2>/dev/null || echo "none")"

if [[ "${REPO_BRANCH}" == "auto" ]]; then
  git fetch --prune --depth 5 origin \
    "+refs/heads/main:refs/remotes/origin/main" \
    "+refs/heads/arena/*:refs/remotes/origin/arena/*"
  TARGET_REF="$(git for-each-ref --sort=-committerdate --format='%(refname:short)' refs/remotes/origin/main refs/remotes/origin/arena/ | head -n 1)"
  [[ -z "${TARGET_REF}" ]] && TARGET_REF="origin/main"
  LOCAL_BRANCH="${TARGET_REF#origin/}"
  git checkout -B "${LOCAL_BRANCH}" "${TARGET_REF}"
  git reset --hard "${TARGET_REF}"
else
  git fetch --depth 1 origin "${REPO_BRANCH}"
  git checkout -B "${REPO_BRANCH}" FETCH_HEAD
  git reset --hard FETCH_HEAD
  LOCAL_BRANCH="${REPO_BRANCH}"
fi

NEW_HEAD="$(git rev-parse HEAD)"
cp -fp "${SECRETS_BACKUP}" "${SECRETS_FILE}"
chmod 600 "${SECRETS_FILE}"

if [[ "${PREV_HEAD}" == "${NEW_HEAD}" ]]; then
  echo "    [OK] Code bereits auf dem allerneuesten Stand: ${LOCAL_BRANCH} @ $(git log -1 --format='%h')"
else
  echo "    [UPDATE] Aktualisiert auf: ${LOCAL_BRANCH} @ $(git log -1 --format='%h — %s')"
fi

# -----------------------------------------------------------------------------
# [Bereich D] PostgreSQL & Redis starten + Datenbank/Rolle pruefen
# -----------------------------------------------------------------------------
echo "==> [D/G] Pruefe PostgreSQL-Datenbank & Redis-Server..."
PG_VER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "${PG_VER}" ]]; then
  pg_ctlcluster "${PG_VER}" main status >/dev/null 2>&1 || pg_ctlcluster "${PG_VER}" main start >/dev/null 2>&1 || service postgresql start >/dev/null 2>&1 || true
else
  service postgresql start >/dev/null 2>&1 || true
fi

if ! redis-cli -h 127.0.0.1 ping 2>/dev/null | grep -q PONG; then
  service redis-server start >/dev/null 2>&1 || redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/supoclip >/dev/null 2>&1 || true
fi
echo "    [OK] PostgreSQL & Redis laufen."

# Rolle & Datenbank 'supoclip' sicherstellen (ohne bestehende Daten zu loeschen!)
su - postgres -c "psql -tc \"SELECT 1 FROM pg_roles WHERE rolname='supoclip'\" | grep -q 1 || psql -c \"CREATE USER supoclip WITH PASSWORD '${PG_PASS}';\"" >/dev/null 2>&1 || true
su - postgres -c "psql -c \"ALTER USER supoclip WITH PASSWORD '${PG_PASS}';\"" >/dev/null 2>&1 || true
su - postgres -c "psql -tc \"SELECT 1 FROM pg_database WHERE datname='supoclip'\" | grep -q 1 || psql -c \"CREATE DATABASE supoclip OWNER supoclip;\"" >/dev/null 2>&1 || true
su - postgres -c "psql -d supoclip -c \"DELETE FROM app_settings WHERE setting_key IN ('LLM', 'RERANK_LLM', 'AI_EDIT_MODEL', 'EDIT_PLAN_LLM', 'OLLAMA_BASE_URL', 'OLLAMA_API_KEY');\"" >/dev/null 2>&1 || true

USER_COUNT="$(su - postgres -c "psql -d supoclip -tAc 'SELECT count(*) FROM \"user\";'" 2>/dev/null | tr -d '[:space:]' || echo "0")"
if [[ "${USER_COUNT}" =~ ^[0-9]+$ ]] && (( USER_COUNT > 0 )); then
  DISABLE_SIGN_UP_VAL="true"
else
  DISABLE_SIGN_UP_VAL="false"
fi

ASYNC_DB_URL="postgresql+asyncpg://supoclip:${PG_PASS}@127.0.0.1:5432/supoclip"
SYNC_DB_URL="postgresql://supoclip:${PG_PASS}@127.0.0.1:5432/supoclip"

# -----------------------------------------------------------------------------
# [Bereich E] Backend Python .venv & Pakete pruefen (NUR reparieren wenn noetig)
# -----------------------------------------------------------------------------
echo "==> [E/G] Pruefe Backend Python-Umgebung (.venv)..."
VENV_OK=0
if [[ "${FORCE_REBUILD}" != "1" && -x "${APP_DIR}/backend/.venv/bin/python" && -x "${APP_DIR}/backend/.venv/bin/uvicorn" && -x "${APP_DIR}/backend/.venv/bin/arq" ]]; then
  if "${APP_DIR}/backend/.venv/bin/python" -c "import fastapi, uvicorn, sqlalchemy, asyncpg, arq, redis, yt_dlp, pydantic_ai, cryptography, apify_client, cv2, numpy, assemblyai, srt" >/dev/null 2>&1; then
    VENV_OK=1
  fi
fi

if [[ "${VENV_OK}" == "1" ]]; then
  echo "    [OK] Python .venv vollstaendig – keine Neuinstallation noetig."
  YTDLP_STAMP="${APP_DIR}/backend/.venv/.ytdlp_updated_at"
  NOW_TS="$(date +%s)"
  LAST_YTDLP_TS="$(cat "${YTDLP_STAMP}" 2>/dev/null || echo 0)"
  if (( NOW_TS - LAST_YTDLP_TS > 86400 )); then
    timeout 45 "${APP_DIR}/backend/.venv/bin/pip" install -q -U yt-dlp >/dev/null 2>&1 && echo "${NOW_TS}" > "${YTDLP_STAMP}" || true
  fi
else
  echo "    [REPAIR] Erstelle/repariere Python .venv & fehlende Backend-Pakete..."
  cd "${APP_DIR}/backend"
  [[ -x .venv/bin/python ]] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip wheel setuptools
  .venv/bin/pip install -q \
    fastapi "uvicorn>=0.27.0" "pydantic-ai>=1.107.7,<2" asyncpg "sqlalchemy>=2.0.25" \
    alembic python-dotenv "yt-dlp[default]" "bgutil-ytdlp-pot-provider==2.0.2" \
    openai-whisper greenlet opencv-python-headless srt-equalizer assemblyai srt \
    numpy aiofiles sse-starlette arq redis boto3 requests cryptography posthog \
    apify-client fonttools httpx
  date +%s > "${APP_DIR}/backend/.venv/.ytdlp_updated_at"
  echo "    [OK] Python .venv erfolgreich eingerichtet."
fi

(
  cd "${APP_DIR}/backend"
  DATABASE_URL="${ASYNC_DB_URL}" SELF_HOST=true "${APP_DIR}/backend/.venv/bin/python" -c "
import asyncio
from src.database import init_db
asyncio.run(init_db())
print('    [OK] Datenbank-Schema verifiziert.')
"
)

# -----------------------------------------------------------------------------
# [Bereich F] Frontend Next.js Standalone & Prisma pruefen
# -----------------------------------------------------------------------------
echo "==> [F/G] Pruefe Next.js Standalone-Frontend & Prisma..."
cd "${APP_DIR}/frontend"

FRONTEND_HASH="$(find "${APP_DIR}/frontend/src" "${APP_DIR}/frontend/messages" "${APP_DIR}/frontend/package.json" -not -path "*/src/generated/*" -type f -exec sha256sum {} + | sort | sha256sum | awk '{print $1}')"
EXISTING_STANDALONE="${APP_DIR}/frontend/.next/standalone"
if [[ -f "${EXISTING_STANDALONE}/frontend/server.js" ]]; then
  EXISTING_STANDALONE="${EXISTING_STANDALONE}/frontend"
fi
EXISTING_PRISMA_SO="$(ls "${EXISTING_STANDALONE}"/src/generated/prisma/libquery_engine-*.so.node 2>/dev/null | head -n1 || true)"

SKIP_FRONTEND_BUILD=0
if [[ "${FORCE_FRONTEND_BUILD}" != "1" \
   && -f "${EXISTING_STANDALONE}/server.js" \
   && -d "${EXISTING_STANDALONE}/.next/static" \
   && -d "${EXISTING_STANDALONE}/public" \
   && -n "${EXISTING_PRISMA_SO}" \
   && -f "${APP_DIR}/frontend/.next/.supoclip_frontend_hash" \
   && "$(cat "${APP_DIR}/frontend/.next/.supoclip_frontend_hash" 2>/dev/null || true)" == "${FRONTEND_HASH}" ]]; then
  SKIP_FRONTEND_BUILD=1
fi

if [[ "${SKIP_FRONTEND_BUILD}" == "1" ]]; then
  echo "    [OK] Frontend-Standalone vollstaendig & aktuell – kein Neubau noetig."
else
  echo "    [BUILD] Baue/aktualisiere Next.js Standalone-Frontend (schonend auf max. ${MAX_CORES} Kernen)..."
  # Pausiere Worker waehrend des Builds fuer freien RAM & CPU
  pkill -f "/opt/supoclip/run_supervisor.sh" >/dev/null 2>&1 || true
  pkill -f "src.workers.run_workers" >/dev/null 2>&1 || true
  pkill -f "src.workers.ai_worker.AIWorkerSettings" >/dev/null 2>&1 || true
  pkill -f "server-request-timeout.cjs" >/dev/null 2>&1 || true
  sleep 1

  if command -v pnpm >/dev/null 2>&1; then
    PNPM_BIN=(pnpm)
  elif command -v corepack >/dev/null 2>&1 && corepack pnpm --version >/dev/null 2>&1; then
    PNPM_BIN=(corepack pnpm)
  else
    npm install -g pnpm@10.27.0
    PNPM_BIN=(pnpm)
  fi

  BUILD_LIMIT_PREFIX=()
  if command -v taskset >/dev/null 2>&1 && taskset -c 0,1 true >/dev/null 2>&1; then
    BUILD_LIMIT_PREFIX+=(taskset -c 0,1)
  fi
  if command -v nice >/dev/null 2>&1 && nice -n 10 true >/dev/null 2>&1; then
    BUILD_LIMIT_PREFIX+=(nice -n 10)
  fi

  LOCK_HASH="$(sha256sum "${APP_DIR}/frontend/package.json" "${APP_DIR}/frontend/pnpm-lock.yaml" "${APP_DIR}/frontend/prisma/schema.prisma" 2>/dev/null | sha256sum | awk '{print $1}')"
  PREV_LOCK_HASH="$(cat "${APP_DIR}/frontend/node_modules/.supoclip_lock_hash" 2>/dev/null || true)"
  SRC_PRISMA_SO="$(ls "${APP_DIR}/frontend/src/generated/prisma"/libquery_engine-*.so.node 2>/dev/null | head -n1 || true)"

  if [[ "${FORCE_REBUILD}" != "1" && "${LOCK_HASH}" == "${PREV_LOCK_HASH}" && -d "${APP_DIR}/frontend/node_modules/next" && -n "${SRC_PRISMA_SO}" ]]; then
    echo "    [FAST] node_modules & Prisma-Engine bereits vorhanden – ueberspringe pnpm install."
  else
    rm -rf /tmp/supoclip_prisma_bak
    if [[ -d "${APP_DIR}/frontend/src/generated/prisma" ]]; then
      cp -rf "${APP_DIR}/frontend/src/generated/prisma" /tmp/supoclip_prisma_bak
    fi

    "${PNPM_BIN[@]}" install --frozen-lockfile --ignore-scripts || "${PNPM_BIN[@]}" install --ignore-scripts
    DATABASE_URL="${SYNC_DB_URL}" "${PNPM_BIN[@]}" exec prisma generate || {
      if [[ -d /tmp/supoclip_prisma_bak ]]; then
        mkdir -p "${APP_DIR}/frontend/src/generated"
        cp -rf /tmp/supoclip_prisma_bak "${APP_DIR}/frontend/src/generated/prisma"
      fi
    }
    rm -rf /tmp/supoclip_prisma_bak
    echo "${LOCK_HASH}" > "${APP_DIR}/frontend/node_modules/.supoclip_lock_hash"
  fi

  DATABASE_URL="${SYNC_DB_URL}" "${PNPM_BIN[@]}" exec prisma db push --skip-generate >/dev/null 2>&1 || true

  if [[ "${FORCE_REBUILD}" == "1" ]]; then
    rm -rf "${APP_DIR}/frontend/.next"
  fi
  cp -f "${APP_DIR}/CHANGELOG.md" "${APP_DIR}/LICENSE" "${APP_DIR}/frontend/" 2>/dev/null || true

  (
    export NODE_ENV=production
    export NEXT_TELEMETRY_DISABLED=1
    export SKIP_LINT=1
    export NODE_OPTIONS="--max-old-space-size=2560"
    export NEXT_PUBLIC_SELF_HOST="true"
    export NEXT_PUBLIC_APP_URL="${PRIMARY_URL}"
    export NEXT_PUBLIC_API_URL="/direct-api"
    export NEXT_PUBLIC_SOCIAL_PUBLISHING="live"
    export DATABASE_URL="${SYNC_DB_URL}"
    export BETTER_AUTH_SECRET="${AUTH_SEC}"
    export BETTER_AUTH_URL="${PRIMARY_URL}"
    export BACKEND_AUTH_SECRET="${BACK_SEC}"
    export BACKEND_INTERNAL_URL="http://127.0.0.1:8000"
    ${BUILD_LIMIT_PREFIX[@]+"${BUILD_LIMIT_PREFIX[@]}"} "${PNPM_BIN[@]}" exec next build --no-lint
  )
  echo "${FRONTEND_HASH}" > "${APP_DIR}/frontend/.next/.supoclip_frontend_hash"

  STANDALONE_DIR="${APP_DIR}/frontend/.next/standalone"
  if [[ -f "${STANDALONE_DIR}/frontend/server.js" ]]; then
    STANDALONE_DIR="${STANDALONE_DIR}/frontend"
  fi

  rm -rf "${STANDALONE_DIR}/public" "${STANDALONE_DIR}/.next/static" "${STANDALONE_DIR}/src/generated/prisma" "${STANDALONE_DIR}/prisma"
  cp -rf "${APP_DIR}/frontend/public" "${STANDALONE_DIR}/public"
  mkdir -p "${STANDALONE_DIR}/.next"
  cp -rf "${APP_DIR}/frontend/.next/static" "${STANDALONE_DIR}/.next/static"
  mkdir -p "${STANDALONE_DIR}/src/generated"
  cp -rf "${APP_DIR}/frontend/src/generated/prisma" "${STANDALONE_DIR}/src/generated/prisma"
  cp -rf "${APP_DIR}/frontend/prisma" "${STANDALONE_DIR}/prisma"
  cp -f "${APP_DIR}/frontend/server-request-timeout.cjs" "${STANDALONE_DIR}/server-request-timeout.cjs"
  cp -f "${APP_DIR}/CHANGELOG.md" "${APP_DIR}/LICENSE" "${STANDALONE_DIR}/" 2>/dev/null || true
  echo "    [OK] Standalone-Frontend fertig gebaut in ${STANDALONE_DIR}"
fi

# -----------------------------------------------------------------------------
# [Bereich G] Supervisor aktualisieren & Dienste starten/pruefen
# -----------------------------------------------------------------------------
echo "==> [G/G] Konfiguriere Supervisor & starte Dienste (Port ${HTTPS_PORT})..."
cat << SUPERVISOR_HEADER > /opt/supoclip/run_supervisor.sh
#!/usr/bin/env bash
set -u
DISABLE_SIGN_UP_DEFAULT="${DISABLE_SIGN_UP_VAL}"
SUPERVISOR_HEADER

cat << 'SUPERVISOR' >> /opt/supoclip/run_supervisor.sh

export PATH="/usr/local/bin:/opt/ffmpeg8/bin:/root/.local/bin:/root/.deno/bin:/usr/bin:/bin:$PATH"
if [ -f /opt/supoclip/.secrets.env ]; then
  # shellcheck disable=SC1091
  source /opt/supoclip/.secrets.env
fi

HTTPS_PORT="${HTTPS_PORT:-1978}"
PG_VER=$(ls /etc/postgresql 2>/dev/null | sort -V | tail -n1 || true)
SSLIP_HOST="${SSLIP_HOST:-${PUBLIC_IP//./-}.sslip.io}"

SSLIP_PORT_URL="https://${SSLIP_HOST}:${HTTPS_PORT}"
SSLIP_STD_URL="https://${SSLIP_HOST}"
HTTP_SSLIP_PORT_URL="http://${SSLIP_HOST}:${HTTPS_PORT}"
IP_PORT_URL="https://${PUBLIC_IP}:${HTTPS_PORT}"
HTTP_IP_PORT_URL="http://${PUBLIC_IP}:${HTTPS_PORT}"
IP_PLAIN_URL="https://${PUBLIC_IP}"

MAIN_URL="${SSLIP_PORT_URL}"
API_URL="/direct-api"
ALL_ORIGINS="${SSLIP_PORT_URL},${SSLIP_STD_URL},${HTTP_SSLIP_PORT_URL},${IP_PORT_URL},${HTTP_IP_PORT_URL},${IP_PLAIN_URL}"

ASYNC_DB_URL="postgresql+asyncpg://supoclip:${PG_PASS}@127.0.0.1:5432/supoclip"
SYNC_DB_URL="postgresql://supoclip:${PG_PASS}@127.0.0.1:5432/supoclip"

STANDALONE_DIR="/opt/supoclip/frontend/.next/standalone"
if [ -f "${STANDALONE_DIR}/frontend/server.js" ]; then
  STANDALONE_DIR="${STANDALONE_DIR}/frontend"
fi
PRISMA_SO=$(ls "${STANDALONE_DIR}"/src/generated/prisma/libquery_engine-*.so.node 2>/dev/null | head -n1 || true)

WORKER_LIMIT_PREFIX=()
if command -v taskset >/dev/null 2>&1 && taskset -c 0,1 true >/dev/null 2>&1; then
  WORKER_LIMIT_PREFIX+=(taskset -c 0,1)
fi
if command -v nice >/dev/null 2>&1 && nice -n 10 true >/dev/null 2>&1; then
  WORKER_LIMIT_PREFIX+=(nice -n 10)
fi

export SELF_HOST=true
export SELF_HOST_VERBOSE_ERRORS=true
export NEXT_PUBLIC_SELF_HOST=true
export NODE_ENV=production
export NEXT_TELEMETRY_DISABLED=1
export PYTHONPATH=/opt/supoclip/backend
export PYTHONUNBUFFERED=1

export SUPOCLIP_MAX_CPU_CORES=2
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export VECLIB_MAXIMUM_THREADS=2
export NUMEXPR_NUM_THREADS=2
export TORCH_NUM_THREADS=2

export DENO_DIR=/var/cache/supoclip-deno
export FX_RUNTIME="${FX_MODE:-off}"
export FX_DENO_BIN=/usr/local/bin/deno
export FX_LIBRARY_DIR=/opt/supoclip/backend/fx_library

export TRANSCRIPTION_PROVIDER=whisper
export WHISPER_MODEL_SIZE=base
export TRANSCRIPTION_LANGUAGE=

export OPENROUTER_API_KEY="${OPENROUTER_KEY:-}"
export LLM="openrouter:openrouter/free"
export RERANK_LLM="openrouter:openrouter/free"
export AI_EDIT_MODEL="openrouter:openrouter/free"
export EDIT_PLAN_LLM="openrouter:openrouter/free"

export SOCIAL_PUBLISHING_ACCESS="all"
export SOCIAL_PUBLISHING_PROVIDERS="youtube,tiktok,instagram,buffer"
export BUFFER_ENABLED="true"
export BUFFER_DEFAULT_MODE="shareNow"
export APP_BASE_URL="${MAIN_URL}"
export SOCIAL_OAUTH_REDIRECT_BASE_URL="${MAIN_URL}"
export SOCIAL_PUBLIC_MEDIA_BASE_URL="${SSLIP_STD_URL}"

export MEDIA_STORAGE=local
export TEMP_DIR=/var/lib/supoclip/uploads
export OUTPUT_DIR=/var/lib/supoclip/clips
export MAX_VIDEO_UPLOAD_BYTES=1500000000
export YOUTUBE_MAX_HEIGHT=1080
export YOUTUBE_SOURCE_CACHE=off
export YOUTUBE_POT_PROVIDER_URL=http://127.0.0.1:4416
export MEDIA_CACHE_TTL_SECONDS=14400
export VIDEO_JOB_USER_CONCURRENCY=1
export WORKFLOWS_ACCESS=all

export REDIS_HOST=127.0.0.1
export REDIS_PORT=6379
export REDIS_PASSWORD=""
export BETTER_AUTH_SECRET="${AUTH_SEC}"
export BETTER_AUTH_URL="${MAIN_URL}"
export BETTER_AUTH_TRUSTED_ORIGINS="${ALL_ORIGINS}"
export BACKEND_AUTH_SECRET="${BACK_SEC}"
export BACKEND_INTERNAL_URL="http://127.0.0.1:8000"
export APP_SETTINGS_ENCRYPTION_KEY="${ENC_SEC}"
export NEXT_PUBLIC_APP_URL="${MAIN_URL}"
export NEXT_PUBLIC_API_URL="${API_URL}"
export CORS_ORIGINS="${ALL_ORIGINS}"
export DISABLE_SIGN_UP="${DISABLE_SIGN_UP_DEFAULT:-true}"
[ -n "$PRISMA_SO" ] && export PRISMA_QUERY_ENGINE_LIBRARY="$PRISMA_SO"

mkdir -p /var/log/supoclip

while true; do
  if [ -n "$PG_VER" ]; then
    pg_ctlcluster "$PG_VER" main status >/dev/null 2>&1 || pg_ctlcluster "$PG_VER" main start >/dev/null 2>&1 || service postgresql start >/dev/null 2>&1 || true
  else
    service postgresql start >/dev/null 2>&1 || true
  fi
  if ! redis-cli -h 127.0.0.1 ping 2>/dev/null | grep -q PONG; then
    service redis-server start >/dev/null 2>&1 || redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/supoclip >/dev/null 2>&1 || true
  fi

  if [ -f /opt/bgutil/server/build/main.js ] && ! pgrep -f "/opt/bgutil/server/build/main.js" >/dev/null 2>&1; then
    (cd /opt/bgutil/server && node build/main.js --host 127.0.0.1 >>/var/log/supoclip/bgutil.log 2>&1 &)
  fi
  if ! pgrep -f "uvicorn src.main:app" >/dev/null 2>&1; then
    (cd /opt/supoclip/backend && SUPOCLIP_MAX_CPU_CORES="" DATABASE_URL="$ASYNC_DB_URL" SUPOCLIP_SERVICE=backend .venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8000 >>/var/log/supoclip/backend.log 2>&1 &)
  fi
  if ! pgrep -f "src.workers.run_workers" >/dev/null 2>&1; then
    (cd /opt/supoclip/backend && ${WORKER_LIMIT_PREFIX[@]+"${WORKER_LIMIT_PREFIX[@]}"} env DATABASE_URL="$ASYNC_DB_URL" SUPOCLIP_SERVICE=worker .venv/bin/python -m src.workers.run_workers >>/var/log/supoclip/worker.log 2>&1 &)
  fi
  if ! pgrep -f "src.workers.ai_worker.AIWorkerSettings" >/dev/null 2>&1; then
    (cd /opt/supoclip/backend && ${WORKER_LIMIT_PREFIX[@]+"${WORKER_LIMIT_PREFIX[@]}"} env DATABASE_URL="$ASYNC_DB_URL" SUPOCLIP_SERVICE=ai-worker .venv/bin/arq src.workers.ai_worker.AIWorkerSettings >>/var/log/supoclip/ai-worker.log 2>&1 &)
  fi
  if ! pgrep -f "server-request-timeout.cjs" >/dev/null 2>&1; then
    (cd "$STANDALONE_DIR" && DATABASE_URL="$SYNC_DB_URL" PORT=3107 HOSTNAME=0.0.0.0 node --require ./server-request-timeout.cjs server.js >>/var/log/supoclip/frontend.log 2>&1 &)
  fi

  sleep 10
done
SUPERVISOR
chmod +x /opt/supoclip/run_supervisor.sh

# Sauberer Neustart der App-Prozesse mit aktueller Konfiguration & neuem Code
pkill -f "/opt/supoclip/run_supervisor.sh" >/dev/null 2>&1 || true
pkill -f "src.workers.run_workers" >/dev/null 2>&1 || true
pkill -f "src.workers.tasks.WorkerSettings" >/dev/null 2>&1 || true
pkill -f "src.workers.cron_worker.CronWorkerSettings" >/dev/null 2>&1 || true
pkill -f "src.workers.ai_worker.AIWorkerSettings" >/dev/null 2>&1 || true
pkill -f "uvicorn src.main:app" >/dev/null 2>&1 || true
pkill -f "server-request-timeout.cjs" >/dev/null 2>&1 || true
sleep 1

nohup /opt/supoclip/run_supervisor.sh >>/var/log/supoclip/supervisor.log 2>&1 </dev/null &

BACKEND_OK=0
FRONTEND_OK=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/health/db >/dev/null 2>&1; then
    BACKEND_OK=1
  fi
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:3107/sign-in || echo "000")"
  if [[ "${HTTP_CODE}" =~ ^(200|302|307|308)$ ]]; then
    FRONTEND_OK=1
  fi
  if [[ "${BACKEND_OK}" == "1" && "${FRONTEND_OK}" == "1" ]]; then
    break
  fi
  sleep 2
done

HOME_BODY="$(curl -sL --max-time 5 http://127.0.0.1:3107/sign-in 2>/dev/null || true)"
if echo "${HOME_BODY}" | grep -q "We couldn't load SupoClip"; then
  echo "[WARNUNG] Frontend meldet SSR-Fehler." >&2
  tail -n 30 /var/log/supoclip/frontend.log 2>/dev/null || true
  exit 1
fi

if [[ "${BACKEND_OK}" != "1" ]]; then
  echo "[WARNUNG] Backend (:8000) antwortet noch nicht." >&2
  tail -n 25 /var/log/supoclip/backend.log 2>/dev/null || true
  exit 1
fi
if [[ "${FRONTEND_OK}" != "1" ]]; then
  echo "[WARNUNG] Frontend (:3107) antwortet noch nicht." >&2
  tail -n 25 /var/log/supoclip/frontend.log 2>/dev/null || true
  exit 1
fi

echo "    [OK] WSL-Dienste aktiv: Backend (:8000) & Frontend (:3107) antworten fehlerfrei."
