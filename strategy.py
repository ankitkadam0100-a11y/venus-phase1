import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))   # all candle times are India time, whatever the computer's clock says
ST_LEN, ST_MULT = 10, 3.0
SESSION_CLOSE = (15, 40)          # last candle of the day is treated as closed after this (Fyers shows a 15:30 candle)
CLOSE_BUFFER_SEC = 5              # candle must be closed at least this long


# ---------- exact TradingView-style building blocks (no pandas_ta) ----------
def heikin_ashi(df):
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    hc = (o + h + l + c) / 4.0
    ho = np.empty_like(hc)
    ho[0] = (o[0] + c[0]) / 2.0
    for i in range(1, len(hc)):
        ho[i] = (ho[i - 1] + hc[i - 1]) / 2.0
    hh = np.maximum.reduce([h, ho, hc])
    hl = np.minimum.reduce([l, ho, hc])
    return pd.DataFrame({"Open": ho, "High": hh, "Low": hl, "Close": hc}, index=df.index)


def rma(x, n):
    """Wilder RMA seeded with SMA, same as Pine ta.rma"""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def ema(x, n):
    """EMA seeded with SMA, same as Pine ta.ema"""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    a = 2.0 / (n + 1)
    out[n - 1] = np.mean(x[:n])
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def supertrend_dir(h, l, c, length=ST_LEN, mult=ST_MULT):
    """Returns direction array: +1 = green/uptrend, -1 = red/downtrend (Pine ta.supertrend)"""
    h, l, c = h.to_numpy(float), l.to_numpy(float), c.to_numpy(float)
    n = len(c)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    atr = rma(tr, length)
    hl2 = (h + l) / 2.0
    bu, bl = hl2 + mult * atr, hl2 - mult * atr
    ub, lb = bu.copy(), bl.copy()
    d = np.full(n, np.nan)
    for i in range(1, n):
        if np.isnan(atr[i]):
            continue
        if np.isnan(atr[i - 1]):                 # first bar with a valid ATR
            d[i] = 1
            continue
        lb[i] = bl[i] if (bl[i] > lb[i - 1] or c[i - 1] < lb[i - 1]) else lb[i - 1]
        ub[i] = bu[i] if (bu[i] < ub[i - 1] or c[i - 1] > ub[i - 1]) else ub[i - 1]
        if np.isnan(d[i - 1]):
            d[i] = 1
        elif d[i - 1] == -1 and c[i] > ub[i - 1]:
            d[i] = 1
        elif d[i - 1] == 1 and c[i] < lb[i - 1]:
            d[i] = -1
        else:
            d[i] = d[i - 1]
    return d


# ---------- closed-candle handling ----------
def _to_dt(v):
    if isinstance(v, (int, float, np.integer, np.floating)) or str(v).isdigit():
        return datetime.fromtimestamp(int(v), IST).replace(tzinfo=None)
    return datetime.strptime(str(v), "%Y-%m-%d %H:%M:%S")


def _candle_end(start, res_min):
    end = start + timedelta(minutes=res_min)
    close = start.replace(hour=SESSION_CLOSE[0], minute=SESSION_CLOSE[1], second=0)
    return min(end, close) if start < close else end   # 15:15 1h bar ends at 15:30


def drop_open_candle(df, res_min, now=None):
    now = now or datetime.now(IST)
    if now.tzinfo is not None:
        now = now.astimezone(IST).replace(tzinfo=None)
    end = _candle_end(_to_dt(df["Datetime"].iloc[-1]), res_min)
    if (now - end).total_seconds() < CLOSE_BUFFER_SEC:
        return df.iloc[:-1].copy()
    return df


def clean_candles(df):
    """Sort by time, drop duplicate timestamps, drop pre-09:15 candles."""
    d = df.copy()
    d["_t"] = d["Datetime"].map(_to_dt)
    d = d.sort_values("_t").drop_duplicates("_t", keep="last")
    mins = d["_t"].dt.hour * 60 + d["_t"].dt.minute
    d = d[mins >= 9 * 60 + 15]          # drop pre-open junk only; the 15:30 candle is real data
    return d.drop(columns="_t").reset_index(drop=True)


# ---------- main entry ----------
def analyse(df, res_min, now=None):
    """
    Returns dict: bearish (state on last CLOSED candle), fresh (state just turned bearish),
    candle_time (start time of that closed candle), plus raw values for debugging.
    """
    if df is None or len(df) < 60:
        return None
    d = clean_candles(df)
    d = drop_open_candle(d, res_min, now).reset_index(drop=True)
    if len(d) < 60:
        return None
    ha = heikin_ashi(d)
    e20, e50 = ema(ha["Close"].to_numpy(), 20), ema(ha["Close"].to_numpy(), 50)
    st = supertrend_dir(ha["High"], ha["Low"], ha["Close"])
    if np.isnan([e20[-1], e50[-1], e20[-2], e50[-2], st[-1], st[-2]]).any():
        return None
    bear = (e50 > e20) & (st == -1)
    return {
        "bearish": bool(bear[-1]),
        "fresh": bool(bear[-1] and not bear[-2]),
        "candle_time": str(_to_dt(d["Datetime"].iloc[-1])),
        "ema20": e20[-1], "ema50": e50[-1], "st_dir": int(st[-1]),
        "ha_close": ha["Close"].iloc[-1],
        "table": pd.DataFrame({"time": d["Datetime"].map(lambda v: str(_to_dt(v))),
                               "ha_close": ha["Close"].round(2), "ema20": np.round(e20, 2),
                               "ema50": np.round(e50, 2), "st_dir": st}).tail(8),
    }
