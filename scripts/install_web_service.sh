#!/usr/bin/env bash
# Generates systemd/weight-monitor-web.service from the template using the
# current user and repo location, installs it, and starts the web UI.
# Safe to re-run after moving the repo or changing users.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"

if [ ! -x "${REPO_DIR}/.venv/bin/wm-web" ]; then
    echo "error: ${REPO_DIR}/.venv/bin/wm-web not found -- create the venv and 'pip install -e .[webui]' first" >&2
    exit 1
fi

sed \
    -e "s|%USER%|${SERVICE_USER}|g" \
    -e "s|%WORKDIR%|${REPO_DIR}|g" \
    "${REPO_DIR}/systemd/weight-monitor-web.service.template" \
    | sudo tee /etc/systemd/system/weight-monitor-web.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now weight-monitor-web

echo "installed and started weight-monitor-web.service (user=${SERVICE_USER}, workdir=${REPO_DIR})"
systemctl status --no-pager weight-monitor-web
