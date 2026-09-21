// PM2 ecosystem config for the trading VPS.
// Processes: hermes gateway, trading loop (paper), nightly backup, heartbeat.
// Start:  pm2 start ecosystem.config.js
// Status: pm2 status | pm2 logs | pm2 restart trading-loop
module.exports = {
  apps: [
    {
      name: "hermes-gateway",
      script: "hermes",
      args: "gateway run",
      cwd: "/opt/hermes-home",
      interpreter: "none",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
      env: { HERMES_HOME: "/opt/hermes-home" },
    },
    {
      name: "trading-loop",
      script: "/opt/trading-agent/venv/bin/python",
      args: "-m trading.allocation.testnet_executor all 100",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // Monthly cadence: run ONCE per day via cron_restart; do NOT autorestart
      // (it exits after the check; autorestart would loop it every minute).
      autorestart: false,
      cron_restart: "5 8 * * *", // 08:05 daily — aligned with Telegram report
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "nightly-backup",
      script: "/opt/trading-agent/scripts/vps-backup.sh",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 3 * * *", // 03:00 daily
    },
    {
      name: "heartbeat",
      script: "/opt/trading-agent/scripts/vps-heartbeat.sh",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 9 * * *", // 09:00 daily -> Telegram ping
    },
  ],
};
