#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import requests
import threading

STATE_FILE = "state.json"
POLY_URL = "https://gamma-api.polymarket.com/markets"
UA = "Mozilla/5.0 (PolymarketAggressiveBot/4.0)"

def env(name: str) -> str:
    return (os.getenv(name) or "").strip()

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"chat_id": "", "last_seen": {}, "last_alert": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"chat_id": "", "last_seen": {}, "last_alert": {}}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)

state = load_state()

# Aggressive defaults
POLL_SECONDS = int(env("POLL_SECONDS") or "30")
LIMIT = int(env("LIMIT") or "300")
MOVE_MIN = float(env("MOVE_MIN") or "0.008")
VOL_DELTA_MIN = float(env("VOL_DELTA_MIN") or "1500")
GAP_CENTS_MIN = float(env("GAP_CENTS_MIN") or "0.5")
COOLDOWN_SECONDS = int(env("COOLDOWN_SECONDS") or "240")

bot = None

def get_chat_id():
    return (state.get("chat_id") or "").strip()

def set_chat_id(cid):
    state["chat_id"] = str(cid).strip()
    save_state(state)

def send(msg: str):
    cid = get_chat_id()
    if not cid or not bot:
        print("[SEND-SKIP]", msg.replace("\n", " | "))
        return
    try:
        bot.send_message(cid, msg, disable_web_page_preview=True)
    except Exception as e:
        print("[TELEGRAM] send failed:", type(e).__name__, str(e))

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
        if s == "yes": i_yes = i
        if s == "no":  i_no = i
    if i_yes is None or i_no is None:
        return None, None
    yes = f(prices[i_yes]); no = f(prices[i_no])
    if not (0 <= yes <= 1 and 0 <= no <= 1):
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

def telegram_loop():
    global bot

    while True:
        token = env("TELEGRAM_TOKEN")
        if (not token) or (":" not in token):
            print("[BOOT] TELEGRAM_TOKEN missing in runtime. Check Railway Variables on THIS service.")
            time.sleep(15)
            continue

        try:
            import telebot
            bot = telebot.TeleBot(token, parse_mode=None)

            @bot.message_handler(commands=["start", "pair"])
            def handle_start(m):
                set_chat_id(m.chat.id)
                bot.reply_to(m, f"✅ Paired to this chat: {m.chat.id}\nNow I’ll send alerts here.")

            @bot.message_handler(commands=["ping"])
            def handle_ping(m):
                bot.reply_to(m, "pong ✅")

            @bot.message_handler(func=lambda m: True, content_types=["text"])
            def handle_any(m):
                if not get_chat_id():
                    set_chat_id(m.chat.id)
                    bot.reply_to(m, f"✅ Paired automatically: {m.chat.id}")
                else:
                    bot.reply_to(m, "✅ bot online. Alerts will come here.")

            print("[BOOT] Telegram polling started.")
            bot.infinity_polling(timeout=30, long_polling_timeout=30)

        except Exception as e:
            print("[POLLING] error:", type(e).__name__, str(e))
            time.sleep(5)

def scanner_loop():
    # Wait until paired
    while not get_chat_id():
        print("[PAIRING] Waiting you to message the bot (/start).")
        time.sleep(10)

    send("🚀 Aggressive bot online (old style: move + volume + gap).")

    while True:
        try:
            markets = fetch_markets()

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
                save_state(state)

        except Exception as e:
            print("[SCAN] error:", type(e).__name__, str(e))
            send(f"⚠️ scan error: {type(e).__name__}: {str(e)[:160]}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    # Start telegram polling in a thread
    t = threading.Thread(target=telegram_loop, daemon=True)
    t.start()

    # Run scanner in main thread
    scanner_loop()
