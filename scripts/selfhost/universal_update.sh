#!/usr/bin/env bash
# =============================================================================
# Universal SupoClip Self-Hosted Self-Heal & Updater Entrypoint
# Works both from /opt/supoclip/scripts/selfhost/ AND standalone from /tmp/!
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "/tmp")"

if [[ -f "${SCRIPT_DIR}/update_vps_buffer.sh" ]]; then
  exec bash "${SCRIPT_DIR}/update_vps_buffer.sh" "$@"
fi

TMP_UPDATER="/tmp/supoclip_update_vps_buffer.sh"
for url in \
  "https://raw.githubusercontent.com/Woolvayne/supoclip/arena/1292b1b9-supoclip/scripts/selfhost/update_vps_buffer.sh" \
  "https://raw.githubusercontent.com/Woolvayne/supoclip/main/scripts/selfhost/update_vps_buffer.sh"; do
  if curl -fsSL --retry 2 --max-time 20 "${url}" -o "${TMP_UPDATER}" 2>/dev/null && grep -q "SupoClip" "${TMP_UPDATER}"; then
    sed -i 's/\r$//' "${TMP_UPDATER}"
    chmod +x "${TMP_UPDATER}"
    exec bash "${TMP_UPDATER}" "$@"
  fi
done

if [[ -f "/opt/supoclip/scripts/selfhost/update_vps_buffer.sh" ]]; then
  exec bash "/opt/supoclip/scripts/selfhost/update_vps_buffer.sh" "$@"
fi

echo "[ERROR] Konnte update_vps_buffer.sh weder lokal noch von GitHub laden." >&2
exit 1
