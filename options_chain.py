"""
Morning job: builds the day's option watchlist (Venus_Watchlist.txt).
Same selection logic as before: 12 underlyings, nearest expiry, 7 strikes around ATM, CE and PE.
Hardened for the cloud: download timeouts and checks, India-time dates, token from environment.
"""
import os
import sys
import time

import pandas as pd
import requests

import venus_common as vc

FILES = {"NSE_FO.csv": "https://public.fyers.in/sym_details/NSE_FO.csv",
         "BSE_FO.csv": "https://public.fyers.in/sym_details/BSE_FO.csv"}
SPOT_SYMBOLS = [
    "NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "BSE:SENSEX-INDEX",
    "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:ICICIBANK-EQ",
    "NSE:SBIN-EQ", "NSE:INFY-EQ", "NSE:TCS-EQ",
    "NSE:AXISBANK-EQ", "NSE:ITC-EQ", "NSE:BHARTIARTL-EQ",
]
BASE_MAP = {
    "NIFTY50": ("NSE", "NIFTY"), "NIFTYBANK": ("NSE", "BANKNIFTY"), "SENSEX": ("BSE", "SENSEX"),
    "RELIANCE": ("NSE", "RELIANCE"), "HDFCBANK": ("NSE", "HDFCBANK"), "ICICIBANK": ("NSE", "ICICIBANK"),
    "SBIN": ("NSE", "SBIN"), "INFY": ("NSE", "INFY"), "TCS": ("NSE", "TCS"),
    "AXISBANK": ("NSE", "AXISBANK"), "ITC": ("NSE", "ITC"), "BHARTIARTL": ("NSE", "BHARTIARTL"),
}


def get_live_spot_prices(fyers):
    print("[1/3] Fetching live spot prices...")
    response = fyers.quotes(data={"symbols": ",".join(SPOT_SYMBOLS)})
    if response.get("code") in (-15, -16):
        raise SystemExit("TOKEN: Fyers token is invalid or expired - login needed")
    if response.get("code") != 200 or "d" not in response:
        raise SystemExit(f"Fyers quotes error: {response}")
    spots = {}
    for q in response["d"]:
        v = q.get("v", {})
        name = (v.get("original_name") or q.get("n") or "").split(":")[-1].split("-")[0]
        if name and v.get("lp"):
            spots[name] = v["lp"]
    missing = [k for k in BASE_MAP if k not in spots]
    if missing:
        print(f"  [!] no spot price for: {missing}")
    print("  -> spot prices secured")
    return spots


def download_master_contracts(force=False):
    print("[2/3] Verifying master contract files...")
    today = vc.now_ist().date()
    for filename, url in FILES.items():
        if not force and os.path.exists(filename):
            if pd.Timestamp(os.path.getmtime(filename), unit="s", tz="UTC").tz_convert(vc.IST).date() == today:
                continue
        print(f"  -> downloading {filename}...")
        r = requests.get(url, timeout=90)
        if r.status_code != 200 or len(r.content) < 100_000 or not r.content[:1].isdigit():
            raise SystemExit(f"Bad download for {filename}: HTTP {r.status_code}, {len(r.content)} bytes. "
                             f"Keeping the old file.")
        tmp = filename + ".tmp"
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, filename)                      # never leaves a half-written file
    print("  -> files are up to date")


def generate_dynamic_watchlist(spot_prices):
    print("[3/3] Calculating dynamic option universe...")
    df = pd.concat([pd.read_csv(f, header=None, names=range(21), low_memory=False)
                    for f in ("NSE_FO.csv", "BSE_FO.csv")])
    df = df[df[16].isin(["CE", "PE"])].copy()
    df = df[[9, 13, 15, 16, 8]]
    df.columns = ["Symbol", "Base", "Strike", "Type", "Expiry"]
    df["Strike"] = pd.to_numeric(df["Strike"], errors="coerce")
    df["Expiry"] = pd.to_numeric(df["Expiry"], errors="coerce")
    df = df.dropna(subset=["Strike", "Expiry"])

    current_epoch = vc.now_ist().timestamp()
    final = []
    for spot_name, spot_price in spot_prices.items():
        if spot_name not in BASE_MAP:
            continue
        exch, base_name = BASE_MAP[spot_name]
        a = df[(df["Base"] == base_name) & (df["Symbol"].str.startswith(f"{exch}:")) & (df["Expiry"] >= current_epoch)]
        if a.empty:
            print(f"  [!] {base_name}: no live contracts")
            continue
        exp = a["Expiry"].min()
        e = a[a["Expiry"] == exp]
        strikes = sorted(e["Strike"].unique())
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot_price))
        target = strikes[max(0, atm - 3):min(len(strikes), atm + 4)]
        syms = e[e["Strike"].isin(target)]["Symbol"].tolist()
        final.extend(syms)
        print(f"  -> {base_name} ({exch}): {len(syms)} contracts")

    if len(final) < 50:
        raise SystemExit(f"Only {len(final)} symbols generated - refusing to overwrite the watchlist")
    with open("Venus_Watchlist.txt.tmp", "w") as f:
        f.write("\n".join(final) + "\n")
    os.replace("Venus_Watchlist.txt.tmp", "Venus_Watchlist.txt")
    print(f"[OK] {len(final)} symbols saved to Venus_Watchlist.txt")
    return final


def main():
    fyers = vc.make_client()
    if fyers is None:
        vc.send_telegram("Venus Phase 1: no Fyers token found - login needed before the market opens.")
        return 2
    try:
        spots = get_live_spot_prices(fyers)
        download_master_contracts()
        wl = generate_dynamic_watchlist(spots)
    except SystemExit as e:
        vc.send_telegram(f"Venus Phase 1 morning job FAILED: {e}")
        return 1
    vc.send_telegram(f"Venus Phase 1: watchlist ready, {len(wl)} option contracts for today.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
