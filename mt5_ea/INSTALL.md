# GoldDipEA — MT5 Installation Guide (Windows)

The EA is at: `/opt/trading-agent/mt5_ea/GoldDipEA.mq5`
(Get this file onto your PC — via Telegram file transfer, email, USB, etc.)

---

## STEP 1 — Copy the EA into MT5's Experts folder

1. Open **MT5** on your PC
2. Menu: **File → Open Data Folder** (opens Windows Explorer at the MT5 data dir)
3. Navigate to: `MQL5 → Experts`
4. Copy **GoldDipEA.mq5** into that `Experts` folder
5. Close the data folder

## STEP 2 — Compile it (turns .mq5 into .ex5)

1. In MT5 menu: **Tools → MetaQuotes Language Editor** (or press **F4**)
2. In MetaEditor: **File → Open** → find `GoldDipEA.mq5` in MQL5/Experts → Open
3. Press **F7** (Compile) — or the Compile button
4. **Watch the "Errors" tab at the bottom**: you want `0 errors, 0 warnings`
   - If there ARE errors, screenshot them and send to me — I'll fix
5. Close MetaEditor

## STEP 3 — Attach EA to XAUUSD chart

1. Back in MT5: make sure **XAUUSD** is in Market Watch
   (right-click Market Watch → Symbols → Metals → XAUUSD → Show)
2. Open an **XAUUSD M15 chart** (click XAUUSD, set timeframe to M15)
3. Drag **GoldDipEA** from the Navigator panel (left side, under Expert Advisors)
   onto the XAUUSD chart
4. A settings dialog opens — **leave defaults** (they're already safe):
   - Risk 0.5% of balance per trade
   - Max 3 trades/day
   - Equity stop $200/day
   - TP 2R, SL below dip low
5. Click **OK**

## STEP 4 — Enable Auto-Trading (the critical switch)

1. In MT5 toolbar, click the **"Algo Trading"** button
   (top-left, looks like a play/running icon) — it must be **GREEN**
2. If it asks: **Tools → Options → Expert Advisors** → tick
   **"Allow algorithmic trading"** → OK
3. The EA name in the chart's top-right corner should show a **smiley face** 🙂
   (not a frown ☹️)

## STEP 5 — Verify it's working

- The **Experts tab** (bottom panel, "Experts" / "Journal") should show:
  `GoldDipEA ready on XAUUSD | balance 10000.00`
- Leave MT5 running (PC on, MT5 open, internet connected)
- When a dip signal fires, you'll see in the Experts tab:
  `BUY 0.02 @ 4450.00 SL ... TP ... dip -0.35% rsi 28`

---

## SAFETY NOTES

- **This is DEMO money** — $10,000 virtual. Nothing real is at risk.
- The EA: 1 position max, SL always set (server-side), TP 2R,
  breakeven lock at +1R, daily trade cap 3, daily equity stop $200,
  NO martingale.
- **Keep MT5 open** for it to trade. PC sleeping = EA sleeping.
- Run it for 20-30 trades, then send me the trade history
  (History tab → right-click → Report) — we'll judge the real win rate.

## TROUBLESHOOTING

| Problem | Fix |
|---|---|
| Frown face 🙂 missing | Algo Trading button off → turn it on (Step 4) |
| "no money" error | Wrong account type — make sure it's the DEMO login |
| Compile errors | Screenshot the Errors tab → send to me |
| No trades for days | Gold not dipping enough — normal, patience. Check Experts tab is alive |
| "Market closed" | Gold closes weekends (Sat 00:00 → Mon 01:00 server) — normal |

---

*EA written 2026-09-09. Demo-first validation: 20-30 trades before any real-money consideration. Send trade reports back for review.*
