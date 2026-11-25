"""
Alert chart for Venus Phase 1: three panels (1h, 30m, 15m) of Heikin Ashi candles with
Supertrend(10,3) (green = up, red = down), EMA20 (light green) and EMA50 (red).
Uses exactly the same candle cleaning and indicator code as the scanner, so the picture matches the alert.
"""
import io

import numpy as np

import strategy

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Rectangle
    HAVE_MPL = True
except Exception:
    HAVE_MPL = False

SHOW = {"1h": 45, "30m": 55, "15m": 70}        # candles drawn per panel
BG, FG, GRID = "#131316", "#d1d4dc", "#2a2e39"


def supertrend_line(h, l, c, length=strategy.ST_LEN, mult=strategy.ST_MULT):
    """Same maths as strategy.supertrend_dir, but also returns the Supertrend LINE (for drawing)."""
    h, l, c = h.to_numpy(float), l.to_numpy(float), c.to_numpy(float)
    n = len(c)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    atr = strategy.rma(tr, length)
    hl2 = (h + l) / 2.0
    bu, bl = hl2 + mult * atr, hl2 - mult * atr
    ub, lb = bu.copy(), bl.copy()
    d = np.full(n, np.nan)
    for i in range(1, n):
        if np.isnan(atr[i]):
            continue
        if np.isnan(atr[i - 1]):
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
    line = np.where(d == 1, lb, ub)
    line[np.isnan(d)] = np.nan
    return d, line


def _prepare(df, res, now):
    if df is None or df.empty:
        return None
    d = strategy.clean_candles(df)
    if d.empty:
        return None
    d = strategy.drop_open_candle(d, res, now).reset_index(drop=True)
    if d.empty:
        return None
    ha = strategy.heikin_ashi(d)
    hc = ha["Close"].to_numpy()
    e20, e50 = strategy.ema(hc, 20), strategy.ema(hc, 50)
    st_dir, st_line = supertrend_line(ha["High"], ha["Low"], ha["Close"])
    times = [strategy._to_dt(v) for v in d["Datetime"]]
    return ha, e20, e50, st_dir, st_line, times


def _fmt(val):
    return f"{val:.2f}" if not np.isnan(val) else "n/a"


def _panel(ax, name, res, df, now, fresh):
    prep = _prepare(df, res, now)
    if prep is None:
        ax.set_facecolor(BG)
        ax.text(0.5, 0.5, f"{name}  |  not enough closed candles", transform=ax.transAxes,
                ha="center", va="center", color=FG, fontsize=9)
        ax.set_xticks([])
        ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_color(GRID)
        return
    ha, e20, e50, st_dir, st_line, times = prep
    n = len(ha)
    lo_i = max(0, n - SHOW[name])
    idx = list(range(lo_i, n))
    o, h, l, c = (ha[k].to_numpy() for k in ("Open", "High", "Low", "Close"))
    for i in idx:
        col = "#26a69a" if c[i] >= o[i] else "#ef5350"
        ax.plot([i, i], [l[i], h[i]], color=col, lw=1, zorder=3)
        ax.add_patch(Rectangle((i - 0.3, min(o[i], c[i])), 0.6, max(abs(c[i] - o[i]), 1e-9), color=col, zorder=3))
    run = []

    def flush():
        if run:
            col = "#1e9e52" if run[0][1] == 1 else "#c0392b"
            ax.plot([r[0] for r in run], [r[2] for r in run], color=col, lw=2.0, zorder=2,
                    marker="o" if len(run) == 1 else None, markersize=4)
        run.clear()
    for i in idx:
        if np.isnan(st_dir[i]):
            flush()
            continue
        if run and run[-1][1] != st_dir[i]:
            flush()
        run.append((i, st_dir[i], st_line[i]))
    flush()
    ax.plot(idx, e20[lo_i:], color="#7bd88f", lw=1.1, zorder=4)
    ax.plot(idx, e50[lo_i:], color="#ff6b6b", lw=1.1, zorder=4)
    core = np.concatenate([l[lo_i:], h[lo_i:], e20[lo_i:][~np.isnan(e20[lo_i:])], e50[lo_i:][~np.isnan(e50[lo_i:])]])
    lo, hi = float(core.min()), float(core.max())
    st_vals = st_line[lo_i:][~np.isnan(st_line[lo_i:])]
    cap = hi + 0.5 * (hi - lo)                              # a far-away Supertrend must not squash the candles
    hi2 = min(max([hi] + list(st_vals)), cap)
    lo2 = max(min([lo] + list(st_vals)), lo - 0.5 * (hi - lo))
    pad = (hi2 - lo2) * 0.06 or 0.1
    ax.set_ylim(lo2 - pad, hi2 + pad)
    ax.set_xlim(lo_i - 1, n + 2)
    last = n - 1
    ax.axvline(last, ls="--", color="#787b86", lw=0.8, zorder=1)
    is_bear = (not np.isnan(e20[-1]) and not np.isnan(e50[-1]) and not np.isnan(st_dir[-1])
               and e50[-1] > e20[-1] and st_dir[-1] == -1)
    if fresh:
        tag, tag_col = "FRESH FLIP (BEARISH)", "#f0b90b"
    elif is_bear:
        tag, tag_col = "bearish", "#ef5350"
    else:
        tag, tag_col = "bullish / neutral", "#9aa0a6"
    ax.text(0.995, 0.96, tag, transform=ax.transAxes, ha="right", va="top", fontsize=9, fontweight="bold",
            color=tag_col)
    ticks = list(range(lo_i, n, 8))
    labels, prev_day = [], None
    for t in ticks:
        dt = times[t]
        labels.append(dt.strftime("%d %b\n%H:%M") if dt.date() != prev_day else dt.strftime("%H:%M"))
        prev_day = dt.date()
    ax.set_xticks(ticks)
    ax.set_xticklabels(labels, color=FG, fontsize=7)
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", colors=FG, labelsize=7)
    ax.grid(color=GRID, lw=0.5, zorder=0)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.set_facecolor(BG)
    ax.set_title(f"{name}  |  Heikin Ashi  |  EMA20 {_fmt(e20[-1])}  EMA50 {_fmt(e50[-1])}  Supertrend {_fmt(st_line[-1])}",
                 color=FG, fontsize=8.5, loc="left")


def make_chart(symbol, ltp, dfs, fresh, now):
    """dfs: {"1h": (60, df), "30m": (30, df), "15m": (15, df)};  fresh: {"1h": bool, ...};  returns PNG bytes."""
    if not HAVE_MPL:
        raise RuntimeError("matplotlib is not installed")
    naive = now.replace(tzinfo=None) if now.tzinfo else now
    fig, axes = plt.subplots(3, 1, figsize=(9.5, 11.5), dpi=100)
    fig.patch.set_facecolor(BG)
    for ax, name in zip(axes, ("1h", "30m", "15m")):
        res, df = dfs[name]
        _panel(ax, name, res, df, naive, fresh.get(name, False))
    trig_list = [n for n in ("1h", "30m", "15m") if fresh.get(n)]
    trig_text = " / ".join(trig_list) if trig_list else "test chart"
    fig.suptitle(f"{symbol}   LTP {ltp:g}   |   Heikin Ashi Bearish Signal: {trig_text}", color=FG,
                 fontsize=11, fontweight="bold", x=0.02, ha="left")
    fig.legend(handles=[Line2D([0], [0], color="#c0392b", lw=2, label="Supertrend red (down)"),
                        Line2D([0], [0], color="#1e9e52", lw=2, label="Supertrend green (up)"),
                        Line2D([0], [0], color="#7bd88f", lw=1.2, label="EMA 20"),
                        Line2D([0], [0], color="#ff6b6b", lw=1.2, label="EMA 50")],
               loc="lower center", ncol=4, fontsize=8, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    fig.tight_layout(rect=(0, 0.03, 1, 0.97))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()