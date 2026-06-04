#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
ENV_FILE="${PROJECT_ROOT}/.env"
ENV_EXAMPLE="${PROJECT_ROOT}/.env.example"

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
  allowed_user_ids="$(ask "Allowed Telegram user IDs, comma-separated. Leave empty to allow all" "")"
  local mini_app_default_chat_id="${allowed_user_ids%%,*}"
  pyro_session="$(ask "Pyrogram session name" "pyrogram_uploader")"

  log "Network and port settings"
  local aria2_host aria2_port aria2_secret dashboard_enable dashboard_host dashboard_port
  local web_app_enable web_app_host web_app_port web_app_url

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

# The Pirate Bay API mirror
# TPB_API_URL=https://apibay.org

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
