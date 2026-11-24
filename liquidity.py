"""Live order-book quality gate (same rules as before, now usable on batched quotes)."""
MIN_VOL_INDEX = 50000
MIN_VOL_STOCK = 2000


def judge_quote(v, is_index=True):
    """v = the 'v' dict of one Fyers quote. Returns (is_liquid, ltp, spread, volume).
    Rules: spread = ask - bid must be > 0 and <= max(Rs 1.00, 1.5% of LTP);
    volume >= 50,000 for index options or 2,000 for stock options."""
    try:
        ltp = v.get("lp", 0) or 0
        volume = v.get("volume", 0) or 0
        bid = v.get("bid", 0) or 0
        ask = v.get("ask", 0) or 0
        spread = ask - bid
        max_allowed_spread = max(1.00, ltp * 0.015)
        if spread > max_allowed_spread or spread <= 0:
            return False, ltp, spread, volume
        if volume < (MIN_VOL_INDEX if is_index else MIN_VOL_STOCK):
            return False, ltp, spread, volume
        return True, ltp, spread, volume
    except Exception:
        return False, 0, 0, 0


def check_option_liquidity(fyers, symbol, is_index=True):
    """Original single-symbol version (kept so the old PC main.py still works)."""
    try:
        response = fyers.quotes(data={"symbols": symbol})
        if response.get("code") == 200 and len(response.get("d", [])) > 0:
            return judge_quote(response["d"][0]["v"], is_index)
        return False, 0, 0, 0
    except Exception as e:
        print(f"Liquidity Check Error for {symbol}: {e}")
        return False, 0, 0, 0
