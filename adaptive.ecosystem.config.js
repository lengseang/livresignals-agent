// Dedicated PM2 config for the adaptive trading engine (paper).
// Cron-style: runs hourly, exits after one cycle, PM2 re-schedules via cron_restart.
// Start: pm2 start adaptive.ecosystem.config.js
module.exports = {
  apps: [
    {
      name: "adaptive-engine",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/adaptive_engine.py check",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "*/10 * * * *", // every 30 minutes
      env: { PYTHONPATH: "/opt/trading-agent/src" },
      out_file: "/opt/trading-agent/logs/adaptive-engine.log",
      error_file: "/opt/trading-agent/logs/adaptive-engine.err.log",
      merge_logs: true,
    },
    {
      name: "weekly-regime-refit",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/weekly_regime_refit.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      autorestart: false,
      cron_restart: "15 9 * * 0", // Sunday 09:15 — re-fit regime edges weekly
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "candle-ingest",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/ingest_all.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // Keeps ALL symbols × timeframes fresh for backtests + the learner.
      autorestart: false,
      cron_restart: "5 * * * *", // hourly at :05
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "system-heartbeat",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/system_heartbeat.py --always",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // Daily proof-of-life. --always so you see it even when healthy.
      autorestart: false,
      cron_restart: "0 9 * * *", // 09:00 daily
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "live-manage",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/live_executor.py manage",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // FAST TP/SL monitor while a position is open (every 10 min).
      // Safe: no-op when no live position; no-op when LIVE_ENABLED absent.
      autorestart: false,
      cron_restart: "*/10 * * * *", // every 10 min
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
    {
      name: "improvement-loop",
      script: "/opt/trading-agent/venv/bin/python",
      args: "/opt/trading-agent/scripts/improvement_loop.py",
      cwd: "/opt/trading-agent",
      interpreter: "none",
      // Weekly self-analysis → ranked suggestions (report → improve → loop).
      autorestart: false,
      cron_restart: "30 9 * * 0", // Sunday 09:30
      env: { PYTHONPATH: "/opt/trading-agent/src" },
    },
  ],
};
