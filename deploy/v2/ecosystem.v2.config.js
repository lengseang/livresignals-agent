// V2 PAPER service only. Start with:  pm2 start deploy/v2/ecosystem.v2.config.js
// This file is SEPARATE from the repo's ecosystem.config.js, which must NOT be started or changed.
// Nothing here places real orders: trading.v2 has no order path (checked by preflight on every start).
module.exports = {
  apps: [
    {
      name: "v2-paper",
      script: "/opt/trading-agent-v2/deploy/v2/run-v2.sh",
      args: "a",
      cwd: "/opt/trading-agent-v2",
      interpreter: "/bin/bash",
      autorestart: true,
      max_restarts: 10,
      min_uptime: 60000,
      restart_delay: 30000,
      exp_backoff_restart_delay: 30000,
      max_memory_restart: "700M",
      kill_timeout: 10000,          // SIGINT first: the runner finishes its record and writes the heartbeat
      time: true,
      out_file: "/opt/trading-agent-v2/logs/v2-paper.out.log",
      error_file: "/opt/trading-agent-v2/logs/v2-paper.err.log",
      merge_logs: true,
    },
    {
      // lane B (owner decision E16): volatility-scaled bracket, separate book and chains, same Jev
      name: "v2-paper-b",
      script: "/opt/trading-agent-v2/deploy/v2/run-v2.sh",
      args: "b",
      cwd: "/opt/trading-agent-v2",
      interpreter: "/bin/bash",
      autorestart: true,
      max_restarts: 10,
      min_uptime: 60000,
      restart_delay: 30000,
      exp_backoff_restart_delay: 30000,
      max_memory_restart: "700M",
      kill_timeout: 10000,
      time: true,
      out_file: "/opt/trading-agent-v2/logs/v2-paper-b.out.log",
      error_file: "/opt/trading-agent-v2/logs/v2-paper-b.err.log",
      merge_logs: true,
    },
    {
      name: "v2-watchdog",
      script: "/opt/trading-agent-v2/deploy/v2/ops-v2.sh",
      args: "watchdog",
      cwd: "/opt/trading-agent-v2",
      interpreter: "/bin/bash",
      autorestart: false,
      cron_restart: "*/5 * * * *",
    },
    {
      name: "v2-daily",
      script: "/opt/trading-agent-v2/deploy/v2/ops-v2.sh",
      args: "daily",
      cwd: "/opt/trading-agent-v2",
      interpreter: "/bin/bash",
      autorestart: false,
      cron_restart: "50 8 * * *",   // 08:50 UTC, before the box's 09:00 heartbeat
    },
  ],
};
