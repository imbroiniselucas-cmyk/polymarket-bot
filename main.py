#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import requests
import telebot

# =============================
# CONFIG
# =============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN").strip()
CHAT_ID = os.getenv("CHAT_ID").strip()

POLL_SECONDS = 30   # bem agressivo
POLY_URL = "https://gamma-api.polymarket.com/markets"

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# =============================
# HELPERS
# =============================
def send(msg):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

def f(x):
    try:
        return float(x)
    except:
        return 0.0

def fetch_markets():
    r = requests.get(
        POLY_URL,
        params={
            "active": "true",
            "closed": "false",
            "limit": 300
        },
        timeout=15
    )
    r.raise_for_status()
    return r.json()

def extract_yes_no(m):
    outcomes = m.get("outcomes", [])
    prices = m.get("outcomePrices", [])
    if len(outcomes) < 2:
        return None, None

    try:
        i_yes = outcomes.index("Yes")
        i_no = outcomes.index("No")
        return f(prices[i_yes]), f(prices[i_no])
    except:
        return None, None

# =============================
# STATE
# =============================
last_seen = {}
last_alert = {}

# =============================
# START
# =============================
send("🚀 Bot agressivo ONLINE — modo antigo ativado")

while True:
    try:
        markets = fetch_markets()

        for m in markets:
            mid = str(m.get("id"))
            if not mid:
                continue

            yes, no = extract_yes_no(m)
            if yes is None:
                continue

            vol = f(m.get("volumeNum"))
            title = m.get("question", "Market")

            prev = last_seen.get(mid)
            last_seen[mid] = (yes, vol)

            if not prev:
                continue

            prev_yes, prev_vol = prev

            # =============================
            # SINAIS AGRESSIVOS
            # =============================
            price_move = yes - prev_yes
            vol_delta = vol - prev_vol
            gap = max(0, 1 - (yes + no)) * 100

            signal = False
            reason = []

            if abs(price_move) >= 0.01:
                signal = True
                reason.append(f"Preço moveu {price_move*100:.2f}%")

            if vol_delta >= 3000:
                signal = True
                reason.append(f"Volume +{int(vol_delta)}")

            if gap >= 0.5:
                signal = True
                reason.append(f"Gap {gap:.1f}¢")

            # cooldown simples
            if signal:
                last = last_alert.get(mid, 0)
                if time.time() - last < 300:
                    continue

                action = "BUY YES" if price_move > 0 else "BUY NO"

                send(
                    f"🚨 ALERTA\n"
                    f"🎯 {action}\n"
                    f"🧠 {' | '.join(reason)}\n"
                    f"YES {yes:.3f} | NO {no:.3f}\n"
                    f"{title}\n"
                    f"https://polymarket.com/market/{m.get('slug','')}"
                )

                last_alert[mid] = time.time()

    except Exception as e:
        send(f"⚠️ Erro: {str(e)[:120]}")

    time.sleep(POLL_SECONDS)
