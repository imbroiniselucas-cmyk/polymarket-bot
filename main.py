#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot (AGGRESSIVE, from scratch, with auto-pairing)

✅ Only required env:
- TELEGRAM_TOKEN=123456:ABC...   (must contain ":")

Optional env:
- CHAT_ID=...                   (if missing/empty, bot will auto-pair)
- POLL_SECONDS=30
- LIMIT=300
- MOVE_MIN=0.008                (0.8pp)
- VOL_DELTA_MIN=1500
- GAP_CENTS_MIN=0.5
- COOLDOWN_SECONDS=240
- PING_EVERY_MIN=20             (sends a light ping so you know it's alive)
"""

import os
import time
import json
import threading
import requests

# telegram library
import telebot

# -------------------------
# ENV / CONFIG
# -------------------------
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID_ENV = (os.getenv("CHAT_ID") or "").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
LIMIT = int(os.getenv("LIMIT", "300"))

MOVE_MIN = float(os.getenv("MOVE_MIN", "0.008"))          # aggressive: 0.8pp move
VOL_DELTA_MIN = float(os.getenv("VOL_DELTA_MIN", "1500")) # aggressive
GAP_CENTS_MIN = float(os.getenv("GAP_CENTS_MIN", "0.5"))
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "240"))

PING_EVERY_MIN = int(os.getenv("PING_EVERY_MIN", "20"))

POLY_URL = "https://gamma-api.polymarket.com/markets"
UA = "Mozilla/5.0 (PolymarketAggressiveBot/3.0)"
STATE_FILE = "state.json"

if (not TELEGRAM_TOKEN) or (":" not in TELEGRAM_TOKEN):
    # Can't do anything without a valid token.
    raise RuntimeError("TELEGRAM_TOKEN missing/invalid. It must look like 123456:ABC... (contains ':').")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# -------------------------
# STATE
# -------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "chat_id": "",
            "last_ping": 0,
            "last_seen": {},   # mid -> [yes, vol]
            "last_alert": {}   # mid -> ts
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("chat_id", "")
        st.setdefault("last_ping", 0)
        st.setdefault("last_seen", {})
        st.setdefault("last_alert", {})
        return st
    except Exception:
        return {"chat_id": "", "last_ping": 0, "last_seen": {}, "last_alert": {}}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

state = load_state()

# If CHAT_ID env is provided, prefer it (and persist it).
if CHAT_ID_ENV:
    state["chat_id"] = CHAT_ID_ENV
    save_state(state)

def get_chat_id():
    return (state.get("chat_id") or "").strip()

def set_chat_id(cid: str):
    state["chat_id"] = str(cid).strip()
    save_state(state)

# -------------------------
# SENDING (safe)
# -------------------------
def send(msg: str):
    cid = get_chat_id()
    if not cid:
        # No chat configured yet: can't send
        print("[PAIRING] No chat_id yet. Waiting user message to pair.")
        print("[MSG]", msg.replace("\n", " | "))
        return
    try:
        bot.send_message(cid, msg, disable_web_page_preview=True)
    except Exception as e:
        # Don't crash; just print
        print("[TELEGRAM] send failed:", type(e).__name__, str(e))
        print("[MSG]", msg.replace("\n", " | "))

# -------------------------
# PAIRING / COMMANDS
# -------------------------
@bot.message_handler(commands=["start", "pair", "id"])
def handle_start(m):
    # Capture chat id automatically
    cid = m.chat.id
    set_chat_id(str(cid))
    bot.reply_to(
        m,
        f"✅ Paired! Chat ID saved: {cid}\n"
        f"Now I'll send alerts here.\n\n"
        f"If you want this in a group: add me to the group and send /pair there."
    )

@bot.message_handler(commands=["ping"])
def handle_ping(m):
    bot.reply_to(m, "pong ✅")

@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_any(m):
    # If not paired, any message pairs
    if not get_chat_id():
        cid = m.chat.id
        set_chat_id(str(cid))
        bot.reply_to(m, f"✅ Paired automatically. Chat ID: {cid}\nI'll start sending alerts.")
    # else ignore normal chatter (bot is for alerts)

def start_polling_thread():
    # polling in a thread so scanner can run in main thread
    def _run():
        while True:
            try:
                bot.infinity_polling(timeout=30, long_polling_timeout=30)
            except Exception as e:
                print("[POLLING] error:", type(e).__name__, str(e))
                time.sleep(5)
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# -------------------------
# POLYMARKET fetch + logic
# -------------------------
def f(x):
    try:
        return float(x)
    except Exception:
        return 0.0

def fetch_markets():
    r = requests.get(
        POLY_URL,
        params={"active": "true", "closed": "false", "limit": LIMIT},
        headers={"User-Agent": UA},
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])

def extract_yes_no(m):
    outcomes = m.get("outcomes") or []
    prices = m.get("outcomePrices") or []
    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None, None
    if len(outcomes) < 2 or len(prices) < 2 or len(outcomes) != len(prices):
        return None, None

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
    if not (0.0 <= yes <= 1.0 and 0.0 <= no <= 1.0):
        return None, None
    return yes, no

def market_url(m):
    slug = (m.get("slug") or "").strip()
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = m.get("conditionId") or m.get("id") or ""
    return f"https://polymarket.com/market/{mid}" if mid else "https://polymarket.com/"

def mid_of(m):
    return str(m.get("id") or m.get("conditionId") or m.get("slug") or "").strip()

# -------------------------
# MAIN
# -------------------------
def scanner_loop():
    # Start message only if paired; otherwise it will show in logs and pair later
    send("🚀 Bot AGRESSIVO ONLINE (auto-pair). Send /pair if needed.")

    while True:
        try:
            markets = fetch_markets()
            sent_now = 0

            for m in markets:
                mid = mid_of(m)
                if not mid:
                    continue

                yes, no = extract_yes_no(m)
                if yes is None:
                    continue

                vol = f(m.get("volumeNum") or m.get("volume") or 0.0)
                title = (m.get("question") or m.get("title") or "Market").strip()

                prev = state["last_seen"].get(mid)
                state["last_seen"][mid] = [yes, vol]

                if not prev:
                    continue

                prev_yes, prev_vol = prev[0], prev[1]

                price_move = yes - prev_yes
                vol_delta = vol - prev_vol
                gap_cents = max(0.0, 1.0 - (yes + no)) * 100.0

                reasons = []
                signal = False

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

                last = float(state["last_alert"].get(mid, 0))
                if (time.time() - last) < COOLDOWN_SECONDS:
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

                state["last_alert"][mid] = time.time()
                sent_now += 1

            # persist state
            save_state(state)

            # lightweight ping so you know it's alive (not every loop)
            if get_chat_id():
                now = time.time()
                last_ping = float(state.get("last_ping", 0))
                if (now - last_ping) >= (PING_EVERY_MIN * 60):
                    send(f"🟣 alive — scanned {len(markets)} markets — alerts now {sent_now}")
                    state["last_ping"] = now
                    save_state(state)

        except Exception as e:
            # no crash loop
            print("[SCAN] error:", type(e).__name__, str(e))
            send(f"⚠️ scan error: {type(e).__name__}: {str(e)[:160]}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    # Start Telegram polling first (pairing + commands)
    start_polling_thread()
    # Run scanner loop
    scanner_loop()
