#!/usr/bin/env bash
# =============================================================================
# SupoClip Self-Hosted Live Updater (WSL 1/2 Ubuntu & Native Ubuntu 22.04+)
# =============================================================================
# Updates an existing /opt/supoclip installation from GitHub without touching
# stored secrets (/opt/supoclip/.secrets.env or backend/.env), ensures static
# FFmpeg 8.1 GPL, activates the native 2-core CPU cap + OpenRouter :free
# smart-router + Buffer GraphQL social publishing, rebuilds the Next.js
# standalone frontend, and restarts services cleanly.
# =============================================================================
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export PNPM_HOME="${PNPM_HOME:-/root/.local/share/pnpm}"
export PATH="/usr/local/bin:/opt/ffmpeg8/bin:${PNPM_HOME}:/pnpm:/root/.local/bin:/root/.deno/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

APP_DIR="${APP_DIR:-/opt/supoclip}"
REPO_URL="${REPO_URL:-https://github.com/Woolvayne/supoclip.git}"
REPO_BRANCH="${REPO_BRANCH:-arena/1292b1b9-supoclip}"
MAX_CORES="${SUPOCLIP_MAX_CPU_CORES:-2}"

if [[ ! -d "${APP_DIR}" ]]; then
  echo "[ERROR] ${APP_DIR} wurde in dieser Linux-Umgebung nicht gefunden." >&2
  exit 1
fi

# 0. Backup & load existing instance secrets (no hardcoded credentials)
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

PUBLIC_IP="${PUBLIC_IP:-$(curl -fsSL --max-time 5 https://api.ipify.org || echo "127.0.0.1")}"
HTTPS_PORT="${HTTPS_PORT:-8443}"
SSLIP_HOST="${SSLIP_HOST:-${PUBLIC_IP//./-}.sslip.io}"
MAIN_URL="https://${SSLIP_HOST}"

echo "==> [1/6] Pruefe statisches FFmpeg 8.1 GPL (fuer tpad=-1 & -fps_mode passthrough)..."
if [[ -f /usr/local/bin/ffmpeg ]] && head -c 2 /usr/local/bin/ffmpeg 2>/dev/null | grep -q '#!'; then
  rm -f /usr/local/bin/ffmpeg
fi
if ! /usr/local/bin/ffmpeg -version 2>/dev/null | grep -qE "version (n?[6789]|8\.)"; then
  mkdir -p /opt/ffmpeg8
  curl -fL --retry 3 \
    "https://github.com/FFmpeg-Builds/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz" \
    -o /tmp/ffmpeg8.tar.xz
  tar -xJf /tmp/ffmpeg8.tar.xz -C /opt/ffmpeg8 --strip-components=1
  rm -f /tmp/ffmpeg8.tar.xz
  ln -sf /opt/ffmpeg8/bin/ffmpeg /usr/local/bin/ffmpeg
  ln -sf /opt/ffmpeg8/bin/ffprobe /usr/local/bin/ffprobe
fi
echo "    [OK] $(/usr/local/bin/ffmpeg -version | head -n 1)"

echo "==> [2/6] Synchronisiere ${APP_DIR} mit ${REPO_URL} (${REPO_BRANCH})..."
git config --global --add safe.directory "${APP_DIR}" >/dev/null 2>&1 || true
cd "${APP_DIR}"
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "${REPO_URL}"
else
  git remote add origin "${REPO_URL}"
fi
git fetch --depth 1 origin "${REPO_BRANCH}"
git checkout -B "${REPO_BRANCH}" FETCH_HEAD
git reset --hard FETCH_HEAD

if [[ ! -f "${SECRETS_FILE}" && -f "${SECRETS_BACKUP}" ]]; then
  cp -fp "${SECRETS_BACKUP}" "${SECRETS_FILE}"
  chmod 600 "${SECRETS_FILE}"
fi

echo "==> [3/6] Pausiere Worker fuer ressourcenschonenden Build & pruefe Datenbank..."
pkill -f "/opt/supoclip/run_supervisor.sh" >/dev/null 2>&1 || true
pkill -f "/opt/supoclip/openrouter_router.py" >/dev/null 2>&1 || true
pkill -f "src.workers.run_workers" >/dev/null 2>&1 || true
pkill -f "src.workers.ai_worker.AIWorkerSettings" >/dev/null 2>&1 || true
pkill -f "uvicorn src.main:app" >/dev/null 2>&1 || true
pkill -f "server-request-timeout.cjs" >/dev/null 2>&1 || true
sleep 1

PG_VER="$(ls /etc/postgresql 2>/dev/null | sort -V | tail -n1 || true)"
if [[ -n "${PG_VER}" ]]; then
  pg_ctlcluster "${PG_VER}" main status >/dev/null 2>&1 || pg_ctlcluster "${PG_VER}" main start >/dev/null 2>&1 || service postgresql start >/dev/null 2>&1 || true
else
  service postgresql start >/dev/null 2>&1 || true
fi

if ! redis-cli -h 127.0.0.1 ping 2>/dev/null | grep -q PONG; then
  service redis-server start >/dev/null 2>&1 || redis-server --daemonize yes --bind 127.0.0.1 --port 6379 --dir /var/lib/supoclip >/dev/null 2>&1 || true
fi

# Clear stale local Ollama overrides in DB so native OpenRouter :free smart-router is used
su - postgres -c "psql -d supoclip -c \"DELETE FROM app_settings WHERE setting_key IN ('LLM', 'RERANK_LLM', 'AI_EDIT_MODEL', 'EDIT_PLAN_LLM', 'OLLAMA_BASE_URL', 'OLLAMA_API_KEY');\"" >/dev/null 2>&1 || true

ASYNC_DB_URL="postgresql+asyncpg://supoclip:${PG_PASS:-supoclip}@127.0.0.1:5432/supoclip"
SYNC_DB_URL="postgresql://supoclip:${PG_PASS:-supoclip}@127.0.0.1:5432/supoclip"

if [[ -x "${APP_DIR}/backend/.venv/bin/python" ]]; then
  (
    cd "${APP_DIR}/backend"
    DATABASE_URL="${ASYNC_DB_URL}" SELF_HOST=true "${APP_DIR}/backend/.venv/bin/python" -c "
import asyncio
from src.database import init_db
asyncio.run(init_db())
print('    [OK] Datenbank-Tabellen & Social-Publishing-Schema bereit.')
"
  )
fi

echo "==> [4/6] Baue Next.js Standalone-Frontend neu (inkl. Buffer-UI & 6 Sprachen)..."
cd "${APP_DIR}/frontend"

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

# Keep a backup of any pre-existing Prisma native query engine before regenerating
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

cp -f "${APP_DIR}/CHANGELOG.md" "${APP_DIR}/LICENSE" "${APP_DIR}/frontend/" 2>/dev/null || true

(
  export NODE_ENV=production
  export NEXT_TELEMETRY_DISABLED=1
  export SKIP_LINT=1
  export NODE_OPTIONS="--max-old-space-size=2560"
  export NEXT_PUBLIC_SELF_HOST="true"
  export NEXT_PUBLIC_APP_URL="${MAIN_URL}"
  export NEXT_PUBLIC_API_URL="/direct-api"
  export NEXT_PUBLIC_SOCIAL_PUBLISHING="live"
  export DATABASE_URL="${SYNC_DB_URL}"
  export BETTER_AUTH_SECRET="${AUTH_SEC:-selfhost-placeholder-secret-32chars}"
  export BETTER_AUTH_URL="${MAIN_URL}"
  export BACKEND_AUTH_SECRET="${BACK_SEC:-selfhost-placeholder-backend-secret}"
  export BACKEND_INTERNAL_URL="http://127.0.0.1:8000"
  ${BUILD_LIMIT_PREFIX[@]+"${BUILD_LIMIT_PREFIX[@]}"} "${PNPM_BIN[@]}" exec next build --no-lint
)

STANDALONE_DIR="${APP_DIR}/frontend/.next/standalone"
if [[ -f "${STANDALONE_DIR}/frontend/server.js" ]]; then
  STANDALONE_DIR="${STANDALONE_DIR}/frontend"
fi

rm -rf "${STANDALONE_DIR}/public" "${STANDALONE_DIR}/.next/static"
cp -rf "${APP_DIR}/frontend/public" "${STANDALONE_DIR}/public"
mkdir -p "${STANDALONE_DIR}/.next"
cp -rf "${APP_DIR}/frontend/.next/static" "${STANDALONE_DIR}/.next/static"
mkdir -p "${STANDALONE_DIR}/src/generated"
cp -rf "${APP_DIR}/frontend/src/generated/prisma" "${STANDALONE_DIR}/src/generated/prisma"
cp -rf "${APP_DIR}/frontend/prisma" "${STANDALONE_DIR}/prisma"
cp -f "${APP_DIR}/frontend/server-request-timeout.cjs" "${STANDALONE_DIR}/server-request-timeout.cjs"
cp -f "${APP_DIR}/CHANGELOG.md" "${APP_DIR}/LICENSE" "${STANDALONE_DIR}/" 2>/dev/null || true
echo "    [OK] Standalone-Frontend aktualisiert in ${STANDALONE_DIR}"

echo "==> [5/6] Aktualisiere Supervisor-Konfiguration (2-Kern-Limit + OpenRouter :free + Buffer)..."
cat << 'SUPERVISOR' > /opt/supoclip/run_supervisor.sh
#!/usr/bin/env bash
set -u

export PATH="/usr/local/bin:/opt/ffmpeg8/bin:/root/.local/bin:/root/.deno/bin:/usr/bin:/bin:$PATH"
if [ -f /opt/supoclip/.secrets.env ]; then
  # shellcheck disable=SC1091
  source /opt/supoclip/.secrets.env
fi

PG_VER=$(ls /etc/postgresql 2>/dev/null | sort -V | tail -n1 || true)
IP_PORT_URL="https://${PUBLIC_IP}:${HTTPS_PORT}"
IP_PLAIN_URL="https://${PUBLIC_IP}"
SSLIP_URL="https://${SSLIP_HOST:-${PUBLIC_IP//./-}.sslip.io}"

if [ -n "${SSLIP_HOST:-}" ]; then
  MAIN_URL="${SSLIP_URL}"
  API_URL="/direct-api"
else
  MAIN_URL="${IP_PORT_URL}"
  API_URL="${IP_PORT_URL}/direct-api"
fi
ALL_ORIGINS="${SSLIP_URL},${IP_PORT_URL},${IP_PLAIN_URL}"

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
export SOCIAL_PUBLIC_MEDIA_BASE_URL="${MAIN_URL}"

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
export DISABLE_SIGN_UP=true
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

# Disable any old Windows Ollama autostart in C:\SupoClip\start_supoclip.ps1 if present
if [[ -f /mnt/c/SupoClip/start_supoclip.ps1 ]]; then
  python3 - <<'PY' || true
import pathlib, re
p = pathlib.Path("/mnt/c/SupoClip/start_supoclip.ps1")
txt = p.read_text(encoding="utf-8", errors="ignore")
txt = re.sub(
    r'\$ollamaOk\s*=\s*\$false.*?if\s*\(-not\s*\$ollamaOk\)\s*\{[^}]*\}',
    'Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue',
    txt,
    flags=re.S,
)
p.write_text(txt, encoding="utf-8")
PY
fi

echo "==> [6/6] Starte SupoClip-Dienste & pruefe Backend + Buffer-API + Frontend..."
WIN_PS="/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
if [[ -x "${WIN_PS}" && -f /mnt/c/SupoClip/start_supoclip.ps1 ]]; then
  "${WIN_PS}" -NoProfile -ExecutionPolicy Bypass -File 'C:\SupoClip\start_supoclip.ps1' >/dev/null 2>&1 || true
else
  nohup /opt/supoclip/run_supervisor.sh >>/var/log/supoclip/supervisor.log 2>&1 </dev/null &
fi

BACKEND_OK=0
FRONTEND_OK=0
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 http://127.0.0.1:8000/health/db >/dev/null 2>&1; then
    BACKEND_OK=1
  fi
  HTTP_CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 http://127.0.0.1:3107/settings/social || echo "000")"
  if [[ "${HTTP_CODE}" =~ ^(200|302|307|308)$ ]]; then
    FRONTEND_OK=1
  fi
  if [[ "${BACKEND_OK}" == "1" && "${FRONTEND_OK}" == "1" ]]; then
    break
  fi
  sleep 2
done

if [[ -x "${APP_DIR}/backend/.venv/bin/python" ]]; then
  (
    cd "${APP_DIR}/backend"
    SOCIAL_PUBLISHING_ACCESS=all SOCIAL_PUBLISHING_PROVIDERS="youtube,tiktok,instagram,buffer" BUFFER_ENABLED=true \
      "${APP_DIR}/backend/.venv/bin/python" -c "
from src.social.registry import list_social_providers
providers = [p.name for p in list_social_providers()]
assert 'buffer' in providers, f'buffer missing in {providers}'
print('    [OK] Aktive Social-Provider:', ', '.join(providers))
"
  )
fi

if [[ "${BACKEND_OK}" != "1" ]]; then
  echo "[WARNUNG] Backend (:8000) antwortet noch nicht. Letzte Logzeilen:" >&2
  tail -n 25 /var/log/supoclip/backend.log 2>/dev/null || true
  exit 1
fi
if [[ "${FRONTEND_OK}" != "1" ]]; then
  echo "[WARNUNG] Frontend (:3107) antwortet noch nicht. Letzte Logzeilen:" >&2
  tail -n 25 /var/log/supoclip/frontend.log 2>/dev/null || true
  exit 1
fi

echo ""
echo "============================================================================"
echo "  [ERFOLG] SupoClip + Buffer Social Publishing ist live!"
echo "  - URL:          ${MAIN_URL}/settings/social"
echo "  - Buffer-Key:   https://publish.buffer.com/settings/api"
echo "  - CPU-Schutz:   max. ${MAX_CORES} Kerne (FFmpeg 8.1 + Whisper + Worker)"
echo "  - KI-Router:    OpenRouter :free Smart-Router (NVIDIA -> Qwen -> Mistral)"
echo "============================================================================"
