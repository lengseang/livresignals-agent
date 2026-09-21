#!/usr/bin/env bash
# Deploy the trading stack to an AWS EC2 (or any SSH) host.
# Usage: ./deploy-to-aws.sh <user@host> [ssh-key-path]
#   example: ./deploy-to-aws.sh ubuntu@1.2.3.4 ~/.ssh/id_ed25519
#
# Steps: bootstrap packages -> rsync project -> place config -> PM2 start.
set -euo pipefail
HOST="${1:?usage: deploy-to-aws.sh user@host [key]}"
KEY="${2:-$HOME/.ssh/id_ed25519}"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"

echo "== [1/4] Bootstrap VPS (packages, node, pm2, python) =="
$SSH "$HOST" 'bash -s' < "$(dirname "$0")/bootstrap-vps.sh"

echo "== [2/4] Create app dirs =="
$SSH "$HOST" 'mkdir -p /opt/trading-agent && mkdir -p /opt/hermes-home'

echo "== [3/4] Sync project (exclude local junk) =="
rsync -avz -e "$SSH" \
  --exclude venv --exclude __pycache__ --exclude .pytest_cache \
  --exclude data/pa-testnet-ledger.json \
  "$(dirname "$0")/../src" "$(dirname "$0")/../config" "$(dirname "$0")/../scripts" \
  "$(dirname "$0")/../data" "$(dirname "$0")/../reports" \
  "$HOST:/opt/trading-agent/" || {
    echo "!! rsync not installed locally — falling back to scp tarball";
    tar -czf /tmp/trading-deploy.tar.gz -C "$(dirname "$0")/.." src config scripts data reports --exclude venv --exclude __pycache__;
    scp -i "$KEY" /tmp/trading-deploy.tar.gz "$HOST:/opt/trading-agent/";
    $SSH "$HOST" 'cd /opt/trading-agent && tar -xzf trading-deploy.tar.gz && rm trading-deploy.tar.gz';
  }

echo "== [4/4] PM2 start =="
$SSH "$HOST" 'cd /opt/trading-agent && cp deploy/ecosystem.config.js /opt/trading-agent/ && pm2 start ecosystem.config.js --only trading-loop --update-env && pm2 save'

echo ""
echo "== DEPLOYED =="
echo "Next: copy Hermes config + keys to /opt/hermes-home, set TESTNET keys,"
echo "then: pm2 start ecosystem.config.js (full stack)."
$SSH "$HOST" 'pm2 status' 2>/dev/null || true