# VPS deploy: shadow scanner + recorder + feed (24/7), terminal viewed from the PC

Release: branch `release/shadow-v1` (shadow stack P2a-c + feed P3/P3b + main with F1-F3), 333 loop tests +
27 live-bot safety tests green on the merged code. **Deploy a pinned commit, never a moving branch.**

## What this adds, and what it must never touch
| Runs 24/7 on the VPS | What it is | Order path |
|---|---|---|
| `livr-recorder` | market-data recorder (kline_1s, trades, book) from the data-stream mirror | none |
| `livr-shadow` | shadow scanner: every candidate + benchmarks as isolated PAPER books, hash-chained records | none |
| `livr-feed` | read-only feed for the terminal, **127.0.0.1 only**, no write routes | none |
| `livr-history.timer` | daily fetch of yesterday's 1m klines (SHA-verified) | none |
| `livr-ticks-retention.timer` | deletes recorder days older than 14 days (~133 MB/day) | none |

It runs as a separate user `livr`, from a separate checkout `/opt/livr-research`, with its own venv and data
dir. It must **not** be able to read the live bot's keys (`/opt/trading-agent/config/*.env`), must not share its
`data/` directory, and gets CPU/memory limits so the live bot always wins. Nothing here imports an exchange
client (test-enforced). The live bot, its crontab and its `.env` are not touched.

## 0. Requirements (check first)
- Ubuntu with systemd; 2 vCPU and ~2 GB RAM free (priming uses 1 core for ~10 min at start/restart).
- Disk: ~1.5 GB history + ~2 GB recorder (14 days) + small shadow records. `df -h /opt`.
- Outbound HTTPS to `data.binance.vision` and WSS to `data-stream.binance.vision`.
- Read access to the GitHub repo for the `livr` user (a read-only deploy key).

## 1. User and isolation
```bash
sudo useradd -m -s /bin/bash livr
sudo mkdir -p /opt/livr-research && sudo chown livr:livr /opt/livr-research
# the research user must NOT be able to read live keys or the live bot's data. Both commands must FAIL:
sudo -u livr cat /opt/trading-agent/config/*.env
sudo -u livr ls /opt/trading-agent/data
```
If either succeeds, fix permissions on `/opt/trading-agent` before going on (for example `chmod o-rwx`).

## 2. Checkout (pinned) and Python 3.12.3
```bash
sudo -u livr -i
git clone git@github.com:lengseang/livresignals-agent.git /opt/livr-research
cd /opt/livr-research
git checkout --detach <PINNED_COMMIT>        # the release commit named in the PR
curl -LsSf https://astral.sh/uv/install.sh | sh && source ~/.local/bin/env
uv python install 3.12.3
uv venv --python 3.12.3 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python --version                 # must print 3.12.3
PYTHONPATH=src .venv/bin/python -m pytest tests/loop -q      # expect all passed
```

## 3. History for priming (~1.5 GB, once)
```bash
cd /opt/livr-research
export PYTHONPATH=src SYMS=BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT,ADAUSDT,LINKUSDT,DOGEUSDT,AVAXUSDT,NEARUSDT,SUIUSDT,ARBUSDT,DOTUSDT,LTCUSDT,ATOMUSDT
.venv/bin/python -m trading.stream.history --monthly klines_1m --since 2023-06 --symbols $SYMS --out data/local/history
.venv/bin/python -m trading.stream.history --kinds klines_1m --days 40 --symbols $SYMS --out data/local/history
```
Every file is checked against Binance's published SHA-256; a failed file is deleted, never used.

## 4. systemd units (as root)
`/etc/systemd/system/livr-recorder.service`
```ini
[Unit]
Description=LIVR market-data recorder (paper research, no orders)
After=network-online.target
[Service]
User=livr
WorkingDirectory=/opt/livr-research
Environment=PYTHONPATH=/opt/livr-research/src
ExecStart=/opt/livr-research/.venv/bin/python -u -m trading.stream.run_ingest --out data/local/ticks
Restart=always
RestartSec=10
Nice=10
CPUQuota=30%
MemoryMax=512M
[Install]
WantedBy=multi-user.target
```
`/etc/systemd/system/livr-shadow.service`
```ini
[Unit]
Description=LIVR shadow scanner (paper books, hash-chained forward records, no orders)
After=network-online.target livr-recorder.service
[Service]
User=livr
WorkingDirectory=/opt/livr-research
Environment=PYTHONPATH=/opt/livr-research/src
ExecStart=/opt/livr-research/.venv/bin/python -u -m trading.shadow.run
Restart=always
RestartSec=60
Nice=10
CPUQuota=60%
MemoryMax=2G
IOSchedulingClass=idle
[Install]
WantedBy=multi-user.target
```
`/etc/systemd/system/livr-feed.service`
```ini
[Unit]
Description=LIVR read-only feed for the terminal (loopback only)
After=network-online.target
[Service]
User=livr
WorkingDirectory=/opt/livr-research
Environment=PYTHONPATH=/opt/livr-research/src
ExecStart=/opt/livr-research/.venv/bin/python -u -m trading.feed.serve --host 127.0.0.1 --port 8765 --data-root data/local
Restart=always
RestartSec=10
Nice=10
CPUQuota=20%
MemoryMax=512M
[Install]
WantedBy=multi-user.target
```
`/etc/systemd/system/livr-history.service` + `.timer` (yesterday's 1m files, 00:30 UTC)
```ini
# livr-history.service
[Unit]
Description=LIVR daily 1m history fetch (SHA-verified)
[Service]
Type=oneshot
User=livr
WorkingDirectory=/opt/livr-research
Environment=PYTHONPATH=/opt/livr-research/src
ExecStart=/opt/livr-research/.venv/bin/python -m trading.stream.history --kinds klines_1m --days 3 --out data/local/history
# livr-history.timer
[Unit]
Description=daily 1m history
[Timer]
OnCalendar=*-*-* 00:30:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
```
`/etc/systemd/system/livr-ticks-retention.service` + `.timer` (**deletes** recorder days older than 14 days;
owner decision, adjust the number before enabling)
```ini
# livr-ticks-retention.service
[Unit]
Description=delete LIVR recorder days older than 14 days
[Service]
Type=oneshot
User=livr
ExecStart=/usr/bin/find /opt/livr-research/data/local/ticks -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} +
# livr-ticks-retention.timer
[Unit]
Description=daily recorder retention
[Timer]
OnCalendar=*-*-* 01:00:00 UTC
Persistent=true
[Install]
WantedBy=timers.target
```
Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now livr-recorder livr-feed livr-history.timer livr-ticks-retention.timer
sudo systemctl enable --now livr-shadow        # primes ~10 min, then goes live
```

## 5. Verify
```bash
journalctl -u livr-shadow -n 20 --no-pager      # prime report, then "alive: ticks=... books=6/6" every 10 min
journalctl -u livr-recorder -n 5 --no-pager
curl -s http://127.0.0.1:8765/v1/health         # feed answers locally
curl -s http://127.0.0.1:8765/v1/shadow | head -c 400
ss -ltnp | grep 8765                            # MUST show 127.0.0.1:8765 only, never 0.0.0.0
```
The shadow books write their first records at the next 00:00 UTC (daily strategies decide once a day).
`data/local/shadow/scorecards.json` updates hourly.

## 6. See it live on the terminal (from the PC, no open port on the VPS)
```bash
ssh -N -L 8765:127.0.0.1:8765 <user>@<vps>      # keep this window open; stop any local paper-feed first
```
Then on the PC, in the front-end repo (branch `loop/terminal-v1`):
```bash
set FEED_URL=http://127.0.0.1:8765&& npx next dev -p 3100
```
Open `http://localhost:3100/terminal`: the league's Forward column, live ticks, ping and signals come from the VPS.
If the PC runs the LIVR-Shadow task too, disable it (`Unregister-ScheduledTask LIVR-Shadow`): the VPS is the one
forward record.

## 7. Update, stop, roll back
```bash
cd /opt/livr-research && sudo -u livr git fetch && sudo -u livr git checkout --detach <NEW_COMMIT>
sudo systemctl restart livr-feed livr-shadow    # a restart is recorded as a gap in every chain
sudo systemctl disable --now livr-shadow livr-feed livr-recorder livr-history.timer livr-ticks-retention.timer
```
Stopping or removing this stack does not affect the live bot.

## Never
- Open port 8765 (or any feed port) to the internet, or bind the feed to 0.0.0.0: the feed has no auth.
- Run these services as root or as the live bot's user; give `livr` access to `config/*.env`.
- Point `--data-root` at the live bot's `data/` directory.
- Deploy an unpinned branch head.
