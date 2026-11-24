"""Shared helpers for Venus Phase 1 (login token, Fyers client, Telegram, India time)."""
import os
import re
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
DRY_RUN = os.getenv("DRY_RUN") == "1"            # testing: print messages instead of sending them


def now_ist():
    """Current India time (timezone-aware). VENUS_NOW='YYYY-MM-DD HH:MM:SS' pins the clock for tests/replays."""
    pinned = os.getenv("VENUS_NOW")
    if pinned:
        return datetime.strptime(pinned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
    return datetime.now(IST)


def app_id():
    return os.getenv("FYERS_APP_ID") or __import__("config").FYERS_APP_ID


def read_token():
    """Daily access token: environment variable first (cloud), then access_token.txt (your PC)."""
    tok = os.getenv("FYERS_ACCESS_TOKEN")
    if tok:
        return tok.strip()
    try:
        with open("access_token.txt") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def make_client():
    from fyers_apiv3 import fyersModel
    tok = read_token()
    if not tok:
        return None
    return fyersModel.FyersModel(client_id=app_id(), token=tok, is_async=False, log_path="")


def _token_problem(token):
    if not token:
        return "TELEGRAM_BOT_TOKEN is missing"
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        return ("TELEGRAM_BOT_TOKEN does not look like a bot token (it should look like 1234567890:AAH...). "
                "On your PC: delete the file venus_bot_token.txt and run the .bat file again.")
    return None


def _net_hint(e):
    t = repr(e)
    if "SSL" in t or "CERTIFICATE_VERIFY_FAILED" in t:
        return ("   -> Your network or antivirus is blocking/inspecting Telegram (common on office Wi-Fi). "
                "Switch to your personal Wi-Fi or phone hotspot and try again.")
    if "Timeout" in t or "timed out" in t:
        return "   -> Telegram did not answer in time (network blocked?). Try personal Wi-Fi or a hotspot."
    return ""


def _status_hint(code):
    if code in (401, 404):
        return "  -> the bot token is wrong or revoked"
    if code in (400, 403):
        return "  -> that person has not tapped Start in the bot"
    return ""


def send_telegram(text):
    """True if at least one recipient got the message. Chat ids are comma separated."""
    print(text, flush=True)
    if DRY_RUN:
        return True
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not token or not chats:
        print("[!] Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    problem = _token_problem(token)
    if problem:
        print(f"[!] {problem}")
        return False
    ok = False
    for chat in chats:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text[:4000]}, timeout=20)
            if r.status_code == 200:
                ok = True
            else:
                print(f"[!] Telegram error for chat {chat}: {r.status_code} {r.text[:150]}{_status_hint(r.status_code)}")
        except Exception as e:
            print(f"[!] Telegram send failed for chat {chat}: {type(e).__name__}")
            hint = _net_hint(e)
            if hint:
                print(hint)
    return ok


def send_photo(png, caption):
    """Sends a PNG chart. True if at least one recipient got it."""
    if DRY_RUN:
        print("[chart image created - not sent in DRY_RUN]")
        return True
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chats = [c.strip() for c in (os.getenv("TELEGRAM_CHAT_ID") or "").split(",") if c.strip()]
    if not token or not chats:
        print("[!] Telegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
        return False
    problem = _token_problem(token)
    if problem:
        print(f"[!] {problem}")
        return False
    ok = False
    for chat in chats:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                              data={"chat_id": chat, "caption": caption[:1000]},
                              files={"photo": ("chart.png", png, "image/png")}, timeout=60)
            if r.status_code == 200:
                ok = True
            else:
                print(f"[!] Telegram photo error for chat {chat}: {r.status_code} {r.text[:150]}{_status_hint(r.status_code)}")
        except Exception as e:
            print(f"[!] Telegram photo failed for chat {chat}: {type(e).__name__}")
            hint = _net_hint(e)
            if hint:
                print(hint)
    return ok