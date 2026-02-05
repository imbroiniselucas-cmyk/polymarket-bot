#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import requests

# Telegram import (safe)
try:
    import telebot
except Exception as e:
    telebot = None
    print("[BOOT] telebot import failed:", repr(e))

# =============================
# CONFIG (aggressive)
# =============================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))  # aggressive
POLY_URL = (os.getenv("POLY_URL") or "https://gamma-api.polymarket.com/markets").strip()

# thresholds (aggressive like old)
MOVE_MIN = float(os.getenv("MOVE_MIN", "0.01"))          # 1pp move in YES
VOL_DELTA_MIN = float(os.getenv("VOL_DELTA_MIN", "3000"))
GAP_CENTS_MIN = float(os.getenv("GAP_CENTS_MIN", "0.5"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "300"))  # 5 min per market

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "15"))
UA = "Mozilla/5.0 (compatible; PolymarketAlertBot/old-aggressive)"

# =============================
# TELEGRAM SETUP (never crash)
# =============================
bot = None
TELEGRAM_OK = False

def init_telegram():
    global bot, TELEGRAM_OK
    if telebot is None:
        print("[BOOT] pyTelegramBotAPI not available. Install dependency: pyTelegramBotAPI")
        TELEGRAM_OK = False
        return
    if not TELEGRAM_TOKEN or ":" not in TELEGRAM_TOKEN:
        print("[BOOT] TELEGRAM_TOKEN missing/invalid at runtime (must contain ':').")
        TELEGRAM_OK = False
        return
    if not CHAT_ID:
        print("[BOOT] CHAT_ID missing at runtime.")
        TELEGRAM_OK = False
        return
    try:
        bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)
        TELEGRAM_OK = True
        print("[BOOT] Telegram OK.")
    except Exception as e:
        print("[BOOT] Telegram init failed:", type(e).__name__, str(e))
        TELEGRAM_OK = False

def send(msg: str):
    # If Telegram isn't OK, print to logs instead of crashing
    if not TELEGRAM_OK or bot is None:
        print("[MSG]", msg.replace("\n", " | "))
        return
    try:
        bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)
    except Exception as e:
        # Don’t crash if Telegram fails
        print("[TELEGRAM] send failed:", type(e).__name__, str(e))
        print("[MSG-FALLBACK]", msg.replace("\n", " | "))

init_telegram()

# =============================
# POLYMARKET HELPERS
# =============================
def f(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def fetch_markets():
    # Keep it simple (old style): just pull a big list of active markets
    r = requests.get(
        POLY_URL,
        params={"active": "true", "closed": "false", "limit": 300},
        headers={"User-Agent": UA},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    # in case API returns wrapper someday
    return data.get("data", []) if isinstance(data, dict) else []

def extract_yes_no(m):
    outcomes = m.get("outcomes") or []
    prices = m.get("outcomePrices") or []
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None, None
    if len(outcomes) < 2 or len(prices) < 2 or len(outcomes) != len(prices):
        return None, None
    # Try to find Yes/No
    i_yes = i_no = None
    for i, o in enumerate(outcomes):
        s = str(o).strip().lower()
        if s == "yes":
            i_yes = i
        elif s == "no":
            i_no = i
    if i_yes is None or i_no is None:
        return None, None
    yes = f(prices[i_yes])
    no = f(prices[i_no])
    # sanity clamp
    if yes < 0 or yes > 1 or no < 0 or no > 1:
        return None, None
    return yes, no

def market_url(m):
    slug = m.get("slug") or ""
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = m.get("conditionId") or m.get("id") or ""
    return f"https://polymarket.com/market/{mid}" if mid else "https://polymarket.com/"

# =============================
# STATE (in-memory, old style)
# =============================
last_seen = {}   # mid -> (yes, vol)
last_alert = {}  # mid -> ts

# =============================
# START
# =============================
send("🚀 Bot agressivo ONLINE — modo antigo (volume + move + gap).")

while True:
    try:
        markets = fetch_markets()
        sent_now = 0

        for m in markets:
            mid = str(m.get("id") or m.get("conditionId") or m.get("slug") or "")
            if not mid:
                continue

            yes, no = extract_yes_no(m)
            if yes is None:
                continue

            vol = f(m.get("volumeNum") or m.get("volume") or 0.0)
            title = (m.get("question") or m.get("title") or "Market").strip()

            prev = last_seen.get(mid)
            last_seen[mid] = (yes, vol)
            if not prev:
                continue

            prev_yes, prev_vol = prev

            price_move = yes - prev_yes
            vol_delta = vol - prev_vol
            gap_cents = max(0.0, 1.0 - (yes + no)) * 100.0

            signal = False
            reasons = []

            if abs(price_move) >= MOVE_MIN:
                signal = True
                reasons.append(f"Move {price_move*100:.2f}pp")

            if vol_delta >= VOL_DELTA_MIN:
                signal = True
                reasons.append(f"VolΔ +{int(vol_delta)}")

            if gap_cents >= GAP_CENTS_MIN:
                signal = True
                reasons.append(f"Gap {gap_cents:.1f}¢")

            if not signal:
                continue

            # Cooldown per market
            t = time.time()
            if (t - last_alert.get(mid, 0)) < COOLDOWN_SECONDS:
                continue

            action = "BUY YES" if price_move > 0 else "BUY NO"

            send(
                f"🚨 ALERTA\n"
                f"🎯 {action}\n"
                f"🧠 {' | '.join(reasons)}\n"
                f"YES {yes:.3f} | NO {no:.3f}\n"
                f"{title[:170]}\n"
                f"{market_url(m)}"
            )

            last_alert[mid] = t
            sent_now += 1

        # If nothing sent, at least show it's alive (log only, not Telegram spam)
        if sent_now == 0:
            print(f"[LOOP] ok. markets={len(markets)} no_alerts. next in {POLL_SECONDS}s")

    except Exception as e:
        # Never crash: log to Railway + (try) telegram
        err = f"{type(e).__name__}: {str(e)[:200]}"
        print("[ERROR]", err)
        send("⚠️ Bot error: " + err)

    time.sleep(POLL_SECONDS)
