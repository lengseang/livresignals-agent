// PM2 ecosystem config for the trading VPS.
// Processes: hermes gateway, trading loop (paper), nightly backup, heartbeat,
// daily signal report.
// Start:  pm2 start ecosystem.config.js
// Status: pm2 status | pm2 logs | pm2 restart trading-loop
module.exports = {
  apps: [
    {
      name: "hermes-gateway",
      script: "/home/ubuntu/hermes-venv/bin/hermes",
      args: "gateway run",
      cwd: "/home/ubuntu/hermes-home",
      interpreter: "none",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 10000,
      env: { HERMES_HOME: "/home/ubuntu/hermes-home", HOME: "/home/ubuntu" },
      out_file: "/home/ubuntu/hermes-home/gateway.log",
      error_file: "/home/ubuntu/hermes-home/gateway.err.log",
    },
    {
      name: "trading-loop",
      script: "/opt/trading-agent/venv/bin/python",
      args: "-m trading.allocation.testnet_executor all 100",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // P-A dual-track: check HOURLY via cron_restart (cadence gate skips until due); do NOT autorestart
      // (it exits after the check; autorestart would loop it every minute).
      autorestart: false,
      cron_restart: "0 * * * *", // hourly — auto trade + manage, 24/7
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
      script: "/opt/trading-agent/scripts/vps-heartbeat-topic.sh",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 9 * * *", // 09:00 daily -> Telegram ping
    },
    {
      name: "signal-report",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/daily_signal_report.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "10 8 * * *", // 08:10 daily — signals + accuracy wrap-up
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "weekly-learning",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/weekly_learning.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "30 9 * * 0", // Sunday 09:30 — adaptive analysis
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "signal-watch",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/signal_watch.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "0 */4 * * *", // every 4h — event-driven signals
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "dual-track-report",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/dual_track_report.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "15 8 * * *", // 08:15 daily — dual-track + improvements
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "dashboard",
      script: "/opt/trading-agent/scripts/dashboard_api.py",
      interpreter: "/opt/trading-agent/venv/bin/python",
      cwd: "/opt/trading-agent",
      autorestart: true,
      max_memory_restart: "300M",
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "adaptive-engine",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/adaptive_engine.py check",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // Regime-adaptive paper trading: run hourly (exits after one cycle; do NOT autorestart).
      autorestart: false,
      cron_restart: "0 * * * *", // hourly — regime detect + capitulation catch
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
  ],
};
