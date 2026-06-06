#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${PROJECT_ROOT}/.env"
VENV_DIR="${PROJECT_ROOT}/.venv"
LOG_FILE="${PROJECT_ROOT}/.cloudflared.log"
TUNNEL_PID=""

log() {
  printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"
}

env_value() {
  local key="$1"
  local default="$2"
  if [[ -f "${ENV_FILE}" ]]; then
    local value
    value="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
    printf '%s' "${value:-$default}"
  else
    printf '%s' "$default"
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "$key" "$value" >> "${ENV_FILE}"
  fi
}

install_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    return
  fi

  log "cloudflared is not installed. Installing it now."
  local arch deb_url tmp_deb
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) deb_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb" ;;
    aarch64|arm64) deb_url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb" ;;
    *)
      log "Unsupported architecture for automatic cloudflared install: ${arch}"
      log "Install cloudflared manually, then rerun this script."
      exit 1
      ;;
  esac

  tmp_deb="$(mktemp --suffix=.deb)"
  if command -v curl >/dev/null 2>&1; then
    curl -L "${deb_url}" -o "${tmp_deb}"
  else
    wget -O "${tmp_deb}" "${deb_url}"
  fi

  if [[ "${EUID}" -eq 0 ]]; then
    dpkg -i "${tmp_deb}" || apt-get install -f -y
  else
    sudo dpkg -i "${tmp_deb}" || sudo apt-get install -f -y
  fi
  rm -f "${tmp_deb}"
}

wait_for_tunnel_url() {
  local url=""
  for _ in $(seq 1 60); do
    url="$(grep -Eo 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' "${LOG_FILE}" | tail -n 1 || true)"
    if [[ -n "${url}" ]]; then
      printf '%s' "${url}"
      return
    fi
    sleep 1
  done

  log "Could not read a Cloudflare tunnel URL from ${LOG_FILE}"
  log "Recent cloudflared output:"
  tail -n 30 "${LOG_FILE}" || true
  exit 1
}

main() {
  cd "${PROJECT_ROOT}"

  if [[ ! -f "${ENV_FILE}" ]]; then
    log ".env was not found. Run scripts/setup_ubuntu.sh first."
    exit 1
  fi

  install_cloudflared

  local web_host web_port tunnel_url
  web_host="$(env_value WEB_APP_HOST 127.0.0.1)"
  web_port="$(env_value WEB_APP_PORT 5000)"

  : > "${LOG_FILE}"
  log "Starting Cloudflare Quick Tunnel to http://${web_host}:${web_port}"
  cloudflared tunnel --url "http://${web_host}:${web_port}" > "${LOG_FILE}" 2>&1 &
  TUNNEL_PID="$!"

  cleanup() {
    if [[ -n "${TUNNEL_PID}" ]] && kill -0 "${TUNNEL_PID}" >/dev/null 2>&1; then
      kill "${TUNNEL_PID}" >/dev/null 2>&1 || true
    fi
  }
  trap cleanup EXIT

  tunnel_url="$(wait_for_tunnel_url)"
  log "Tunnel URL: ${tunnel_url}"
  log "Updating WEB_APP_URL in .env so Telegram opens the mini-app through HTTPS."
  set_env_value WEB_APP_ENABLE true
  set_env_value WEB_APP_HOST "${web_host}"
  set_env_value WEB_APP_PORT "${web_port}"
  set_env_value WEB_APP_URL "${tunnel_url}"

  if [[ ! -d "${VENV_DIR}" ]]; then
    log "Virtual environment not found. Run scripts/setup_ubuntu.sh first."
    exit 1
  fi

  # shellcheck disable=SC1091
  source "${VENV_DIR}/bin/activate"
  log "Starting bot. Use /start in Telegram after startup to refresh the Mini-App button."
  python main.py
}

main "$@"
