//+------------------------------------------------------------------+
//| GoldDipEA.mq5 — 15m gold dip-buy with RSI capitulation           |
//|                                                                  |
//| Logic (mirrors the server-side gold_scanner):                    |
//|   * On a NEW M15 bar:                                            |
//|     dip = close / highest-high(last 90 bars) - 1                 |
//|     RSI(14) on closed bars                                       |
//|   * BUY when dip <= -0.25% AND RSI <= 32 AND RSI turning up      |
//|   * SL below the dip low (90-bar low - buffer)                   |
//|   * TP = 2R (twice the risk distance) — server-side stops        |
//|   * Breakeven when +1R, time-exit after InpHoldHours if green    |
//|                                                                  |
//| Risk: fixed % of balance per trade, 1 position max, hard daily   |
//| trade cap, equity stop. NO martingale, NO averaging.             |
//|                                                                  |
//| SAFETY: demo-first. Run on your $10k demo; validate 20-30 trades |
//| before any real-money thought.                                   |
//+------------------------------------------------------------------+
#property copyright "Hermes"
#property version   "1.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

//--- inputs ---------------------------------------------------------
input double InpRiskPct        = 0.5;    // Risk % of balance per trade
input double InpDipPct         = 0.25;   // Min dip % from 90-bar high (0.25 = 0.25%)
input double InpRSI            = 32.0;   // RSI(14) capitulation threshold
input double InpSLBufferPct    = 0.10;   // Extra buffer below dip low (% of price)
input double InpTPRR           = 2.0;    // TP = RR * SL distance
input int    InpMaxTradesPerDay = 3;     // Hard daily trade cap
input double InpEquityStopUSD  = 200.0;  // Stop trading if equity drops this much today
input int    InpHoldHours      = 12;     // Time-exit green trades after N hours
input long   InpMagic          = 99201;  // EA magic number
input int    InpLookback       = 90;     // Dip lookback bars

//--- globals ---------------------------------------------------------
int      g_rsiHandle = INVALID_HANDLE;
double   g_rsiBuf[];
datetime g_lastBar   = 0;
double   g_startEquity = 0.0;
int      g_dayTrades = 0;
int      g_lastDay   = -1;
double   g_dayStartBalance = 0.0;

//+------------------------------------------------------------------+
int OnInit()
{
   trade.SetExpertMagicNumber(InpMagic);
   // must match broker filling mode or market orders get rejected
   trade.SetTypeFillingBySymbol(_Symbol);
   g_rsiHandle = iRSI(_Symbol, PERIOD_M15, 14, PRICE_CLOSE);
   if(g_rsiHandle == INVALID_HANDLE)
   {
      Print("RSI handle failed");
      return(INIT_FAILED);
   }
   ArraySetAsSeries(g_rsiBuf, true);
   g_startEquity = AccountInfoDouble(ACCOUNT_BALANCE);
   g_dayStartBalance = g_startEquity;
   Print("GoldDipEA ready on ", _Symbol, " | balance ", DoubleToString(g_startEquity, 2));
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) { }

//+------------------------------------------------------------------+
//| Track daily trade count (resets on UTC day change)               |
//+------------------------------------------------------------------+
void RefreshDayState()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day != g_lastDay)
   {
      g_lastDay = dt.day;
      g_dayTrades = 0;
      g_dayStartBalance = AccountInfoDouble(ACCOUNT_BALANCE);
   }
}

int TradesToday()
{
   RefreshDayState();
   return g_dayTrades;
}

bool HasOpenPosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return true;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Main tick handler — act once per NEW closed bar                   |
//+------------------------------------------------------------------+
void OnTick()
{
   // only act when a new M15 bar has formed
   datetime curBar = iTime(_Symbol, PERIOD_M15, 0);
   if(curBar == g_lastBar) return;
   g_lastBar = curBar;

   // --- manage existing position first (breakeven / time-exit) ---
   ManagePosition();

   // --- safety checks before opening ---
   if(HasOpenPosition()) return;
   RefreshDayState();
   if(g_dayTrades >= InpMaxTradesPerDay) return;

   double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(g_dayStartBalance - equity >= InpEquityStopUSD)
   {
      Print("Equity stop hit for the day (lost >= $", InpEquityStopUSD, ") — standing down");
      return;
   }

   // weekend filter: gold market closed Sat/Sun
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day_of_week == 0 || dt.day_of_week == 6) return;

   // --- compute signal on CLOSED bars ---
   double closes[];
   double highs[];
   double lows[];
   ArraySetAsSeries(closes, true);
   ArraySetAsSeries(highs, true);
   ArraySetAsSeries(lows, true);

   int need = InpLookback + 10;
   if(CopyClose(_Symbol, PERIOD_M15, 0, need, closes) < need) return;
   if(CopyHigh(_Symbol, PERIOD_M15, 1, InpLookback, highs) < InpLookback) return;
   if(CopyLow(_Symbol, PERIOD_M15, 1, InpLookback, lows) < InpLookback) return;

   // highest high of last InpLookback bars (shift 1..InpLookback)
   double hi = highs[ArrayMaximum(highs, 0, InpLookback)];
   // lowest low
   double lo = lows[ArrayMinimum(lows, 0, InpLookback)];

   double lastClose = closes[1];           // last CLOSED bar close
   double dipPct    = (lastClose / hi - 1.0) * 100.0;

   // RSI at last closed bar and 2 bars before (turning check)
   int got = CopyBuffer(g_rsiHandle, 0, 1, 3, g_rsiBuf);
   if(got < 3) return;
   double rsiNow = g_rsiBuf[0];
   double rsiPrev = g_rsiBuf[1];
   bool turning = (rsiNow > rsiPrev);

   // --- SIGNAL ---
   if(dipPct <= -InpDipPct && rsiNow <= InpRSI && turning)
   {
      // entry = current ask (market buy)
      double ask = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
      double sl  = lo * (1.0 - InpSLBufferPct / 100.0);
      double tp  = ask + (ask - sl) * InpTPRR;

      // position size from risk: $risk / $per-point per lot / SL distance
      double riskUSD  = AccountInfoDouble(ACCOUNT_BALANCE) * InpRiskPct / 100.0;
      double contract = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE); // oz per lot (100)
      double lotStep  = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
      double minLot   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
      double maxLot   = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);

      double lots = 0.0;
      if(ask - sl > 0 && contract > 0)
         lots = riskUSD / ((ask - sl) * contract);
      lots = MathFloor(lots / lotStep) * lotStep;
      if(lots < minLot) lots = minLot;
      if(lots > maxLot) lots = maxLot;

      // sanity: SL distance must be >= ~0.05% of price (spread protection)
      double slPct = (ask - sl) / ask * 100.0;
      if(slPct < 0.05)
      {
         Print("SL too tight (", DoubleToString(slPct, 3), "%) — skip");
         return;
      }

      if(trade.Buy(lots, _Symbol, 0.0, sl, tp, "GoldDipEA"))
      {
         g_dayTrades++;
         Print("BUY ", DoubleToString(lots, 2), " @ ", DoubleToString(ask, 2),
               " SL ", DoubleToString(sl, 2), " TP ", DoubleToString(tp, 2),
               " dip ", DoubleToString(dipPct, 2), "% rsi ", DoubleToString(rsiNow, 1));
      }
      else
      {
         Print("Buy failed: ", trade.ResultRetcodeDescription());
      }
   }
}

//+------------------------------------------------------------------+
//| Manage open position: breakeven at +1R, time-exit green after 12h|
//+------------------------------------------------------------------+
void ManagePosition()
{
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         PositionGetInteger(POSITION_MAGIC) != InpMagic) continue;

      double openPx = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl     = PositionGetDouble(POSITION_SL);
      double tp     = PositionGetDouble(POSITION_TP);
      double curPx  = SymbolInfoDouble(_Symbol, SYMBOL_BID);
      double rDist  = openPx - sl;   // initial risk distance (long)

      // breakeven once price moved +1R in our favor
      if(rDist > 0 && sl < openPx && curPx >= openPx + rDist && tp > 0)
      {
         // move SL to entry + tiny buffer (lock breakeven)
         double newSl = openPx + rDist * 0.05;
         if(MathAbs(newSl - sl) > 0.01)
         {
            trade.PositionModify(ticket, newSl, tp);
            Print("Breakeven locked @ ", DoubleToString(newSl, 2));
         }
         return; // position protected; let it run to TP
      }

      // time-exit: green but no TP after InpHoldHours -> bank it
      datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
      double ageH = (TimeCurrent() - openTime) / 3600.0;
      if(ageH >= InpHoldHours && curPx > openPx)
      {
         trade.PositionClose(ticket);
         Print("Time-exit green position after ", DoubleToString(ageH, 1), "h");
      }
      return;
   }
}
//+------------------------------------------------------------------+
