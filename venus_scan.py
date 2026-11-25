#!/usr/bin/env python3
"""
Venus Phase 1 - SINGLE-PASS scanner (Independent 15m / 30m / 1h Heikin Ashi signals).
Runs once per 15-minute candle (on GitHub, or on your PC), then exits. Places no orders. Alert only.

Rule:
  Each timeframe (15m, 30m, 1h) is evaluated INDEPENDENTLY on closed Heikin Ashi candles.
  Whenever ANY timeframe has EMA50 > EMA20 and Supertrend(10,3) red AND just flipped bearish
  on its last closed candle (and the contract passes liquidity + MIN_LTP), an alert fires once.

API efficiency (Fyers Standard: 50/min, 5,000/day):
  * Liquidity is checked FIRST with batched quotes (50 symbols per call).
  * One 60-day 15m history call is fetched per liquid symbol; exact 09:15 IST-anchored
    30m and 1h candles are resampled from it in memory (1 API call instead of 3).
"""
import csv
import json
import os
import sys
import time
from collections import deque
from datetime import timedelta

import pandas as pd

import liquidity
import strategy
import venus_chart
import venus_common as vc

STATE_DIR = os.getenv("STATE_DIR", "state")
STATE_FILE = os.path.join(STATE_DIR, "phase1_state.json")
ALERT_CSV = os.path.join(STATE_DIR, "Live_Alerts.csv")
WATCHLIST = os.getenv("WATCHLIST_FILE", "Venus_Watchlist.txt")
FORCE = os.getenv("FORCE") == "1"                       # ignore the market-hours guard (tests / after-hours checks)
PLAN = os.getenv("FYERS_PLAN", "standard").lower()      # "standard" (free) or "prime"
PACE = os.getenv("PACE", "1") == "1"                    # PACE=0 only for tests
MIN_LTP = float(os.getenv("MIN_LTP", "5"))              # ignore options priced below this (Rs); 0 = off
CHARTS = os.getenv("CHARTS", "1") == "1"                # send a 3-panel chart with each alert
MAX_CHARTS = int(os.getenv("MAX_CHARTS", "5"))          # charts per scan (the rest are listed as text)
HISTORY_DAYS = int(os.getenv("HISTORY_DAYS", "60"))     # single 15m fetch (Fyers allows up to 100 days)

# Stay safely below Fyers' published limits (Standard: 5/s, 50/min, 5,000/day; Prime: 10/s, 500/min, 500,000/day)
LIMITS = {"standard": {"per_sec": 4, "per_min": 45, "per_day": 4500, "quote_batch": 50},
          "prime": {"per_sec": 8, "per_min": 450, "per_day": 400000, "quote_batch": 500}}
# Kept for compatibility with send_test_chart.py / helper scripts
HISTORY = (("1h", "60", 60), ("30m", "30", 40), ("15m", "15", 25))


class TokenExpired(Exception):
    pass


class RateLimited(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class Api:
    """Every Fyers call goes through here: pacing, daily budget, error codes."""

    def __init__(self, fyers, state):
        lim = LIMITS.get(PLAN, LIMITS["standard"])
        self.fyers, self.state, self.lim = fyers, state, lim
        self.times = deque()
        self.session_calls = 0

    def _pace(self):
        if not PACE:
            return
        while True:
            now = time.monotonic()
            while self.times and now - self.times[0] > 60:
                self.times.popleft()
            last_sec = sum(1 for t in self.times if now - t < 1)
            if len(self.times) < self.lim["per_min"] and last_sec < self.lim["per_sec"]:
                break
            time.sleep(0.05)
        self.times.append(time.monotonic())

    def call(self, fn, data=None):
        if self.state["calls"] >= self.lim["per_day"]:
            raise BudgetExceeded()
        self._pace()
        try:
            resp = fn(data=data) if data is not None else fn()
        except Exception as e:
            self.state["calls"] += 1
            return {"code": -999, "s": "error", "message": repr(e)}
        self.state["calls"] += 1
        self.session_calls += 1
        code = resp.get("code") if isinstance(resp, dict) else None
        if code in (-15, -16):
            raise TokenExpired()
        if code in (-360, -429):
            raise RateLimited(str(resp.get("message", "")))
        return resp


# ----------------------------------------------------------------- state
def load_state(now):
    today = now.strftime("%Y-%m-%d")
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except Exception:
        st = {}
    if st.get("date") != today:                       # new trading day: counters and flags reset
        st.update({"date": today, "calls": 0, "status_sent": False, "token_alert": False,
                   "limit_alert": False, "scans": 0, "alerts": 0})
    st.setdefault("fired", [])
    cutoff = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    st["fired"] = [k for k in st["fired"] if k.split("|")[-1][:10] >= cutoff]
    return st


def save_state(st):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def log_alert(candle_time, symbol, ltp, spread, volume, trigger_msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    new = not os.path.exists(ALERT_CSV)
    with open(ALERT_CSV, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Timestamp", "Symbol", "LTP", "Spread", "Volume", "Setup"])
        w.writerow([candle_time, symbol, round(ltp, 2), round(spread, 2), volume,
                    f"Bearish HA Signal (Trigger: {trigger_msg})"])


# ----------------------------------------------------------------- data
def history_df(api, symbol, resolution, days_back, now):
    data = {"symbol": symbol, "resolution": str(resolution), "date_format": "1",
            "range_from": (now - timedelta(days=days_back)).strftime("%Y-%m-%d"),
            "range_to": now.strftime("%Y-%m-%d"), "cont_flag": "0"}
    resp = api.call(api.fyers.history, data)
    if resp.get("s") == "ok" and resp.get("candles"):
        df = pd.DataFrame(resp["candles"], columns=["Datetime", "Open", "High", "Low", "Close", "Volume"])
        df = df[df["Datetime"] <= now.timestamp()]            # candles that have not started yet do not exist (replays)
        return df.reset_index(drop=True) if len(df) else None
    return None


def resample_ohlcv(df_15m, res_min):
    """Build exact NSE/BSE 30m or 1h OHLCV candles from 15m candles (anchored at 09:15 IST)."""
    if df_15m is None or df_15m.empty:
        return None
    d = strategy.clean_candles(df_15m)
    if d.empty:
        return None
    buckets = []
    for raw_t in d["Datetime"]:
        dt = strategy._to_dt(raw_t)
        offset = (dt.hour * 60 + dt.minute) - (9 * 60 + 15)
        b_min = (offset // res_min) * res_min
        b_dt = dt.replace(hour=9, minute=15, second=0, microsecond=0) + timedelta(minutes=b_min)
        buckets.append(int(b_dt.replace(tzinfo=vc.IST).timestamp()))
    g = d.assign(_bucket=buckets).groupby("_bucket", sort=True, as_index=False).agg(
        Open=("Open", "first"),
        High=("High", "max"),
        Low=("Low", "min"),
        Close=("Close", "last"),
        Volume=("Volume", "sum"),
    )
    return g.rename(columns={"_bucket": "Datetime"})


def market_closed_today(api, now):
    """True only if Fyers says the NSE F&O segment is CLOSED before the afternoon (holiday). Unknown -> False."""
    if now.hour * 60 + now.minute >= 15 * 60 + 30:
        return False
    try:
        for m in api.call(api.fyers.market_status).get("marketStatus", []):
            if m.get("exchange") == 10 and m.get("segment") == 11:
                return str(m.get("status", "")).upper() == "CLOSED"
    except (TokenExpired, RateLimited, BudgetExceeded):
        raise
    except Exception:
        pass
    return False


def in_market_window(now):
    m = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 + 25 <= m <= 15 * 60 + 50


# ----------------------------------------------------------------- scan
def scan(api, watch, state, now):
    naive_now = now.replace(tzinfo=None)
    # 1) liquidity first, 50 symbols per call
    liquid = {}
    batch = api.lim["quote_batch"]
    for i in range(0, len(watch), batch):
        resp = api.call(api.fyers.quotes, {"symbols": ",".join(watch[i:i + batch])})
        for q in resp.get("d", []) if isinstance(resp, dict) else []:
            sym = q.get("n") or q.get("v", {}).get("symbol")
            if not sym or q.get("s") not in (None, "ok"):
                continue
            ok, ltp, spread, vol = liquidity.judge_quote(q.get("v", {}), "NIFTY" in sym or "SENSEX" in sym)
            if ok and ltp >= MIN_LTP:
                liquid[sym] = (ltp, spread, vol)

    # 2) evaluate 1h, 30m, and 15m INDEPENDENTLY from a single 15m fetch per liquid symbol
    found = []
    for sym in watch:
        if sym not in liquid:
            continue
        df_15m = history_df(api, sym, "15", HISTORY_DAYS, now)
        if df_15m is None:
            continue
        dfs = {
            "1h": (60, resample_ohlcv(df_15m, 60)),
            "30m": (30, resample_ohlcv(df_15m, 30)),
            "15m": (15, df_15m),
        }
        tfs = {}
        new = []
        for name in ("1h", "30m", "15m"):
            res, df = dfs[name]
            r = strategy.analyse(df, res, naive_now) if df is not None else None
            if r is None:
                continue
            tfs[name] = r
            if r["fresh"] and f"{sym}|{name}|{r['candle_time']}" not in state["fired"]:
                new.append((name, r["candle_time"]))
        if not new:
            continue
        # Show the finest triggered timeframe's closed candle time (new is ordered 1h, 30m, 15m)
        candle_time = new[-1][1]
        fresh_map = {n: any(t == n for t, _ in new) for n in ("1h", "30m", "15m")}
        found.append((sym, new, candle_time, liquid[sym], dfs, fresh_map))
    return found, len(liquid)


def run():
    now = vc.now_ist()
    if not FORCE and not in_market_window(now):
        print(f"[{now:%a %H:%M}] outside market hours - nothing to do")
        return 0
    fyers = vc.make_client()
    if fyers is None:
        vc.send_telegram("Venus Phase 1: no Fyers token found - login needed.")
        return 2
    state = load_state(now)
    api = Api(fyers, state)
    try:
        if not os.path.exists(WATCHLIST):
            vc.send_telegram("Venus Phase 1: Venus_Watchlist.txt is missing - the morning job has not run.")
            return 3
        watch = [l.strip() for l in open(WATCHLIST) if l.strip()]
        if not FORCE and market_closed_today(api, now):
            print("Market is closed today (holiday) - nothing to do")
            save_state(state)
            return 0
        found, n_liquid = scan(api, watch, state, now)
        state["scans"] = state.get("scans", 0) + 1
        lines = []
        for sym, new, candle, (ltp, spread, vol), dfs, fresh in found:
            trig = "/".join(n for n, _ in new)
            lines.append((sym, new, candle, ltp, spread, vol, trig, dfs, fresh))
        if lines:
            head = f"VENUS PHASE 1 | Heikin Ashi bearish | scan {now:%H:%M} IST | {len(lines)} alert(s)"
            body = [f"{s} | LTP {l:g} | spread {sp:g} | vol {v:,} | TF {t} | candle {c[11:16]}"
                    for s, _, c, l, sp, v, t, _d, _f in lines]
            if vc.send_telegram(head + "\n" + "\n".join(body)):
                for s, new, c, l, sp, v, t, _d, _f in lines:
                    log_alert(c, s, l, sp, v, t)
                    for n, ct in new:
                        state["fired"].append(f"{s}|{n}|{ct}")
                state["alerts"] = state.get("alerts", 0) + len(lines)
                if CHARTS and venus_chart.HAVE_MPL:
                    for s, new, c, l, sp, v, t, dfs_, fresh_ in lines[:MAX_CHARTS]:
                        try:
                            png = venus_chart.make_chart(s, l, dfs_, fresh_, now)
                            vc.send_photo(png, f"{s} | LTP {l:g} | spread {sp:g} | vol {v:,} | TF {t} | candle {c[11:16]}")
                            try:
                                os.makedirs("charts", exist_ok=True)
                                with open(os.path.join("charts", f"{s.split(':')[-1]}_{c[11:16].replace(':', '')}.png"), "wb") as fh:
                                    fh.write(png)
                            except OSError as e:
                                print(f"[i] could not save the chart file ({e}) - the alert was still sent")
                        except Exception as e:
                            print(f"[!] chart failed for {s}: {e!r}")
                elif CHARTS:
                    print("[i] matplotlib is not installed - alerts are sent as text only (pip install matplotlib)")
            else:
                print("[!] alerts not delivered - they will be retried on the next run")
        if not state.get("status_sent"):
            if vc.send_telegram(f"Venus Phase 1 is running (independent 15m/30m/1h HA). Watching {len(watch)} contracts, "
                                f"{n_liquid} liquid now. API calls this scan: {api.session_calls}."):
                state["status_sent"] = True
        print(f"scan done: {len(watch)} watched, {n_liquid} liquid, {len(found)} alerts, "
              f"{api.session_calls} calls ({state['calls']} today)")
        save_state(state)
        return 0
    except TokenExpired:
        if not state.get("token_alert"):
            if vc.send_telegram("Venus Phase 1: Fyers token is invalid or expired - login needed."):
                state["token_alert"] = True
        save_state(state)
        return 2
    except RateLimited as e:
        if not state.get("limit_alert"):
            if vc.send_telegram(f"Venus Phase 1: Fyers rate limit hit - scan stopped to avoid a day block. {e}"):
                state["limit_alert"] = True
        save_state(state)
        return 4
    except BudgetExceeded:
        if not state.get("limit_alert"):
            if vc.send_telegram(f"Venus Phase 1: daily API budget ({api.lim['per_day']} calls) used up - "
                                f"scanning paused until tomorrow."):
                state["limit_alert"] = True
        save_state(state)
        return 4


if __name__ == "__main__":
    sys.exit(run())