#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
ENV_FILE="${PROJECT_ROOT}/.env"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.example"
PROWLARR_AUTO_API_KEY=""
PROWLARR_AUTO_URL=""

log() {
  printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"
}

ask() {
  local prompt="$1"
  local default="${2:-}"
  local value

  if [[ -n "${default}" ]]; then
    read -r -p "${prompt} [${default}]: " value
    printf '%s' "${value:-$default}"
  else
    read -r -p "${prompt}: " value
    printf '%s' "$value"
  fi
}

ask_secret() {
  local prompt="$1"
  local value
  read -r -s -p "${prompt}: " value
  printf '\n' >&2
  printf '%s' "$value"
}

ask_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local answer
  read -r -p "${prompt} [${default}]: " answer
  answer="${answer:-$default}"
  [[ "${answer,,}" =~ ^(y|yes)$ ]]
}

ask_user_ids() {
  local value sanitized chunk
  while true; do
    value="$(ask "Allowed Telegram numeric user IDs, comma-separated. Do NOT paste bot token. Leave empty to allow all" "")"
    value="${value// /}"
    if [[ -z "${value}" ]]; then
      printf ''
      return
    fi

    sanitized=""
    IFS=',' read -ra chunks <<< "${value}"
    for chunk in "${chunks[@]}"; do
      if [[ -z "${chunk}" ]]; then
        continue
      fi
      if [[ ! "${chunk}" =~ ^[0-9]+$ ]]; then
        log "Invalid user ID '${chunk}'. Telegram user IDs are numbers only, for example 123456789."
        log "Bot tokens contain a colon and must only be entered at the BotFather token prompt."
        sanitized=""
        break
      fi
      if [[ -n "${sanitized}" ]]; then
        sanitized+=","
      fi
      sanitized+="${chunk}"
    done

    if [[ -n "${sanitized}" ]]; then
      printf '%s' "${sanitized}"
      return
    fi
  done
}

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  else
    python3 -c 'import secrets; print(secrets.token_hex(24))'
  fi
}

require_ubuntu() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    if [[ "${ID:-}" != "ubuntu" && "${ID_LIKE:-}" != *"ubuntu"* && "${ID_LIKE:-}" != *"debian"* ]]; then
      log "This script is tuned for Ubuntu/Debian. Continuing anyway."
    fi
  fi
}

install_system_packages() {
  local packages=(
    aria2
    build-essential
    curl
    ffmpeg
    git
    libgl1
    libglib2.0-0
    p7zip-full
    python3
    python3-dev
    python3-pip
    python3-venv
    unzip
    zip
  )

  if ! command -v apt-get >/dev/null 2>&1; then
    log "apt-get was not found. Install these packages manually: ${packages[*]}"
    return
  fi

  log "Installing Ubuntu packages: ${packages[*]}"
  if [[ "${EUID}" -eq 0 ]]; then
    apt-get update
    apt-get install -y "${packages[@]}"
  else
    sudo apt-get update
    sudo apt-get install -y "${packages[@]}"
  fi
}

create_venv() {
  if [[ ! -d "${VENV_DIR}" ]]; then
    log "Creating Python virtual environment at ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}"
  else
    log "Using existing virtual environment at ${VENV_DIR}"
  fi

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  python -m pip install --upgrade pip
}

install_python_requirements() {
  log "Installing Python requirements"
  pip install -r "${PROJECT_ROOT}/requirements.txt"
}

read_prowlarr_api_key() {
  local config_file="$1"
  if [[ ! -f "${config_file}" ]]; then
    return 1
  fi

  python3 - "${config_file}" <<'PY'
import sys
import xml.etree.ElementTree as ET

config_file = sys.argv[1]
try:
    root = ET.parse(config_file).getroot()
except Exception:
    raise SystemExit(1)

api_key = (root.findtext("ApiKey") or "").strip()
if not api_key:
    raise SystemExit(1)
print(api_key)
PY
}

install_prowlarr_optional() {
  if ! ask_yes_no "Install Prowlarr with Docker if Docker is already available?" "n"; then
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    log "Docker was not found. Skipping Prowlarr install."
    log "Install Prowlarr manually, then set PROWLARR_URL and PROWLARR_API_KEY in .env."
    return
  fi

  local config_dir="${PROJECT_ROOT}/.prowlarr"
  local config_file="${config_dir}/config.xml"
  mkdir -p "${config_dir}"
  log "Starting Prowlarr on http://127.0.0.1:9696"

  if docker ps -a --format '{{.Names}}' | grep -qx 'telegram-aio-prowlarr'; then
    docker start telegram-aio-prowlarr >/dev/null
  else
    docker run -d \
      --name telegram-aio-prowlarr \
      --restart unless-stopped \
      -p 127.0.0.1:9696:9696 \
      -e PUID="$(id -u)" \
      -e PGID="$(id -g)" \
      -e TZ="${TZ:-UTC}" \
      -v "${config_dir}:/config" \
      lscr.io/linuxserver/prowlarr:latest >/dev/null
  fi

  log "Waiting for Prowlarr to generate its API key"
  for _ in $(seq 1 60); do
    if PROWLARR_AUTO_API_KEY="$(read_prowlarr_api_key "${config_file}" 2>/dev/null)"; then
      PROWLARR_AUTO_URL="http://127.0.0.1:9696"
      log "Captured Prowlarr API key automatically."
      log "Open http://127.0.0.1:9696 later to add indexers."
      return
    fi
    sleep 2
  done

  PROWLARR_AUTO_API_KEY=""
  log "Prowlarr started, but the API key was not available yet."
  log "Open http://127.0.0.1:9696, finish first-run setup if needed, then paste the API key when prompted."
}

write_env() {
  if [[ -f "${ENV_FILE}" ]] && ! ask_yes_no ".env already exists. Replace it?" "n"; then
    log "Keeping existing .env"
    return
  fi

  log "Collecting Telegram credentials"
  local bot_token api_id api_hash allowed_user_ids pyro_session
  bot_token="$(ask_secret "BotFather bot token")"
  api_id="$(ask "Telegram API_ID from my.telegram.org")"
  api_hash="$(ask_secret "Telegram API_HASH from my.telegram.org")"
  allowed_user_ids="$(ask_user_ids)"
  local mini_app_default_chat_id="${allowed_user_ids%%,*}"
  pyro_session="$(ask "Pyrogram session name" "pyrogram_uploader")"

  log "Network and port settings"
  local aria2_host aria2_port aria2_secret dashboard_enable dashboard_host dashboard_port
  local web_app_enable web_app_host web_app_port web_app_url
  local prowlarr_url prowlarr_api_key prowlarr_limit

  if ask_yes_no "Use automatic local-only ports and hosts?" "y"; then
    aria2_host="127.0.0.1"
    aria2_port="6800"
    aria2_secret="$(generate_secret)"
    dashboard_enable="true"
    dashboard_host="127.0.0.1"
    dashboard_port="8080"
    web_app_enable="true"
    web_app_host="127.0.0.1"
    web_app_port="5000"
    web_app_url="http://127.0.0.1:5000"
    prowlarr_url="${PROWLARR_AUTO_URL:-http://127.0.0.1:9696}"
    prowlarr_api_key="${PROWLARR_AUTO_API_KEY:-}"
    prowlarr_limit="20"
  else
    aria2_host="$(ask "aria2 RPC host" "127.0.0.1")"
    aria2_port="$(ask "aria2 RPC port" "6800")"
    aria2_secret="$(ask_secret "aria2 RPC secret. Leave empty for generated local token")"
    dashboard_enable="$(ask "Enable web dashboard? true/false" "true")"
    dashboard_host="$(ask "Dashboard host" "127.0.0.1")"
    dashboard_port="$(ask "Dashboard port" "8080")"
    web_app_enable="$(ask "Enable Telegram mini-app? true/false" "true")"
    web_app_host="$(ask "Mini-app host" "127.0.0.1")"
    web_app_port="$(ask "Mini-app port" "5000")"
    web_app_url="$(ask "Mini-app public URL. Use HTTPS/ngrok/domain for Telegram production" "http://${web_app_host}:${web_app_port}")"
    prowlarr_url="$(ask "Prowlarr URL" "http://127.0.0.1:9696")"
    prowlarr_api_key="$(ask_secret "Prowlarr API key. Leave empty to configure later")"
    prowlarr_limit="$(ask "Prowlarr search result limit" "20")"
  fi

  if [[ -z "${prowlarr_api_key}" ]]; then
    prowlarr_api_key="$(ask_secret "Prowlarr API key. Leave empty to configure later")"
  else
    log "Using automatically captured Prowlarr API key."
  fi

  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  cat > "${ENV_FILE}" <<EOF
# Telegram Bot Configuration
BOT_TOKEN=${bot_token}
API_ID=${api_id}
API_HASH=${api_hash}

# Pyrogram user session used for large uploads
PYRO_SESSION_NAME=${pyro_session}

# Security: comma-separated Telegram user IDs. Empty means allow all users.
ALLOWED_USER_IDS=${allowed_user_ids}

# External tools
ARIA2_BIN=aria2c
ARIA2_RPC_HOST=${aria2_host}
ARIA2_RPC_PORT=${aria2_port}
# Optional. Leave empty to let the bot generate a local daemon token.
ARIA2_RPC_SECRET=${aria2_secret}
FFMPEG_BIN=ffmpeg
SPOTDL_BIN=spotdl

# The Pirate Bay API mirror
# TPB_API_URL=https://apibay.org
# Optional RARBG-style clone base URL. Default: https://rargb.to
# RARBG_BASE_URL=https://rargb.to

# Prowlarr multi-indexer search
PROWLARR_URL=${prowlarr_url}
PROWLARR_API_KEY=${prowlarr_api_key}
PROWLARR_SEARCH_LIMIT=${prowlarr_limit}

# Runtime
APP_ENV=development
AUTO_CLEANUP_DAYS=7

# Web dashboard
WEB_DASHBOARD_ENABLE=${dashboard_enable}
WEB_DASHBOARD_HOST=${dashboard_host}
WEB_DASHBOARD_PORT=${dashboard_port}

# Telegram mini-app
WEB_APP_ENABLE=${web_app_enable}
WEB_APP_HOST=${web_app_host}
WEB_APP_PORT=${web_app_port}
WEB_APP_URL=${web_app_url}
# Optional fallback for mini-app upload/zip if Telegram WebApp initData is missing.
MINI_APP_DEFAULT_CHAT_ID=${mini_app_default_chat_id}
EOF

  chmod 600 "${ENV_FILE}"
  log "Wrote ${ENV_FILE}"
}

main() {
  cd "${PROJECT_ROOT}"
  require_ubuntu
  install_system_packages
  install_prowlarr_optional
  create_venv
  install_python_requirements
  write_env

  log "Setup complete."
  printf '\nNext steps:\n'
  printf '  source .venv/bin/activate\n'
  printf '  python main.py\n'
  printf '\nFor Telegram Mini-App without a domain, use HTTPS tunnel startup instead:\n'
  printf '  bash scripts/start_with_cloudflare_tunnel.sh\n'
}

main "$@"
