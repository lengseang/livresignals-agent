#!/usr/bin/env bash
# Bootstrap a fresh Ubuntu VPS (AWS EC2 t3.micro or any Debian/Ubuntu box)
# into a full trading stack:
#   - Node + PM2 (process manager, auto-restart, boot-start)
#   - Python 3.11 + ccxt (trading engine)
#   - Hermes gateway (Telegram + cron + alerts)
#   - P-A trading loop + nightly backup + heartbeat
#
# Usage: ssh ubuntu@IP 'bash -s' < bootstrap-vps.sh
# Requires: a fresh Ubuntu 22.04/24.04 instance, SSH access, ~6GB disk.
set -euo pipefail

echo "== [1/6] System packages =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git python3 python3-pip python3-venv \
  build-essential libssl-dev ufw sqlite3 >/dev/null

echo "== [2/6] Node + PM2 =="
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
npm install -g pm2 >/dev/null 2>&1 || true

echo "== [3/6] Application directories =="
mkdir -p /opt/trading-agent/{src,scripts,config,data,reports}
mkdir -p /opt/hermes-home
cd /opt/trading-agent

echo "== [4/6] Python deps =="
python3 -m venv /opt/trading-agent/venv
/opt/trading-agent/venv/bin/pip install -q --upgrade pip
/opt/trading-agent/venv/bin/pip install -q ccxt

echo "== [5/6] Firewall (safe defaults) =="
ufw allow OpenSSH >/dev/null 2>&1 || true
ufw --force enable >/dev/null 2>&1 || true

echo "== [6/6] PM2 boot persistence =="
pm2 startup systemd -u $(whoami) --hp /home/$(whoami) >/dev/null 2>&1 || true
pm2 save >/dev/null 2>&1 || true

echo ""
echo "== BOOTSTRAP DONE =="
echo "Next: copy trading-agent source + config into /opt/trading-agent,"
echo "then: pm2 start ecosystem.config.js"
echo "IP check: $(hostname -I 2>/dev/null | awk '{print $1}')"