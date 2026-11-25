#!/usr/bin/env bash
# Universal SupoClip Self-Hosted Updater Entrypoint
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/update_vps_buffer.sh" "$@"
