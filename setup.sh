#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BIN="${HOME}/.local/bin"
SERVICE_SRC="${PROJECT_DIR}/systemd/wind-server.service"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_DST="${SERVICE_DIR}/wind-server.service"
RC_BEGIN="# >>> wind-server setup >>>"
RC_END="# <<< wind-server setup <<<"

# Detect the user's login shell and pick the appropriate rc file.
_detect_rc_file() {
  local shell_name
  shell_name="$(basename "${SHELL:-/bin/bash}")"
  case "${shell_name}" in
    *zsh*) echo "${HOME}/.zshrc" ;;
    *)     echo "${HOME}/.bashrc" ;;
  esac
}
RC_FILE="$(_detect_rc_file)"

log() {
  printf '\033[1;32m[wind-server setup]\033[0m %s\n' "$*"
}

warn() {
  printf '\033[1;33m[wind-server setup]\033[0m %s\n' "$*" >&2
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    warn "Missing required command: $1"
    exit 1
  fi
}

install_package() {
  require_command python3
  mkdir -p "${LOCAL_BIN}"

  if command -v pipx >/dev/null 2>&1; then
    if pipx list 2>/dev/null | grep -q 'package wind-server '; then
      log "Upgrading existing pipx install from ${PROJECT_DIR}"
      pipx install --force "${PROJECT_DIR}"
    else
      log "Installing with pipx from ${PROJECT_DIR}"
      pipx install "${PROJECT_DIR}"
    fi
    return
  fi

  warn "pipx not found; falling back to user editable install with pip"
  python3 -m pip install --user -e "${PROJECT_DIR}"
}

update_rc_file() {
  touch "${RC_FILE}"

  local block
  block=$(cat <<'EOF'
# >>> wind-server setup >>>
export PATH="$HOME/.local/bin:$PATH"
alias wind-server-ui='wind-server ui'
alias wind-server-status='wind-server status'
# <<< wind-server setup <<<
EOF
)

  if grep -Fq "${RC_BEGIN}" "${RC_FILE}"; then
    log "Updating existing wind-server block in ${RC_FILE}"
    python3 - "${RC_FILE}" "${RC_BEGIN}" "${RC_END}" "${block}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
begin = sys.argv[2]
end = sys.argv[3]
block = sys.argv[4]
text = path.read_text()
start = text.index(begin)
finish = text.index(end, start) + len(end)
path.write_text(text[:start].rstrip() + "\n" + block + "\n" + text[finish:].lstrip())
PY
  else
    log "Adding wind-server block to ${RC_FILE}"
    printf '\n%s\n' "${block}" >> "${RC_FILE}"
  fi
}

setup_systemd_service() {
  if [[ ! -f "${SERVICE_SRC}" ]]; then
    warn "Service file not found: ${SERVICE_SRC}"
    return
  fi

  if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemctl not found; skipping user service setup"
    return
  fi

  mkdir -p "${SERVICE_DIR}"
  cp "${SERVICE_SRC}" "${SERVICE_DST}"

  if systemctl --user daemon-reload >/dev/null 2>&1; then
    if systemctl --user enable --now wind-server.service >/dev/null 2>&1; then
      log "Enabled and started user service: wind-server.service"
    else
      warn "Could not enable/start wind-server.service; run manually: systemctl --user enable --now wind-server.service"
    fi
  else
    warn "Could not reload user systemd; run manually: systemctl --user daemon-reload"
  fi
}

verify_install() {
  if command -v wind-server >/dev/null 2>&1; then
    log "Installed: $(command -v wind-server)"
  elif [[ -x "${LOCAL_BIN}/wind-server" ]]; then
    log "Installed: ${LOCAL_BIN}/wind-server"
  else
    warn "wind-server is not on PATH yet. Run: source ${RC_FILE}"
  fi
}

main() {
  log "Setting up wind-server from ${PROJECT_DIR}"
  install_package
  update_rc_file
  setup_systemd_service
  verify_install
  log "Done. Restart your shell or run: source ${RC_FILE}"
  log "Try: wind-server status"
}

main "$@"
