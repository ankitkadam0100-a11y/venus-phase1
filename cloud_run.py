"""
Cloud entry point (GitHub Actions). One run = (build today's watchlist if it is not built yet) + ONE scan.
The cron-job.org timer starts it every 15 minutes on market days. Places no orders.
"""
import os
import shutil
import sys

import venus_common as vc

STATE = os.getenv("STATE_DIR", "state")
WL = os.path.join(STATE, "Venus_Watchlist.txt")
WL_DATE = os.path.join(STATE, "watchlist_date.txt")
os.environ.setdefault("WATCHLIST_FILE", WL)                   # venus_scan reads this when it is imported

import options_chain  # noqa: E402
import venus_scan  # noqa: E402


def watchlist_is_today(today):
    try:
        return open(WL_DATE).read().strip() == today and os.path.exists(WL)
    except FileNotFoundError:
        return False


def main():
    now = vc.now_ist()
    os.makedirs(STATE, exist_ok=True)
    if now.weekday() >= 5 and os.getenv("FORCE") != "1":
        print("Weekend - nothing to do")
        return 0
    today = now.strftime("%Y-%m-%d")
    if not watchlist_is_today(today):
        fyers = vc.make_client()
        if fyers is None:
            vc.send_telegram("Venus Phase 1: no Fyers token found in GitHub - run morning_login.bat on your PC.")
            return 2
        try:
            if os.getenv("FORCE") != "1" and venus_scan.market_closed_today(venus_scan.Api(fyers, {"calls": 0}), now):
                print("Market is closed today (holiday) - nothing to do")
                return 0
        except venus_scan.TokenExpired:
            vc.send_telegram("Venus Phase 1: Fyers token is invalid or expired - run morning_login.bat on your PC.")
            return 2
        except Exception as e:
            print("market status check skipped:", repr(e))
        rc = options_chain.main()                             # builds Venus_Watchlist.txt + sends 'watchlist ready'
        if rc != 0:
            return rc
        shutil.copy("Venus_Watchlist.txt", WL)
        with open(WL_DATE, "w") as f:
            f.write(today)
    return venus_scan.run()


if __name__ == "__main__":
    sys.exit(main())
