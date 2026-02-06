#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
POLYMARKET EDGE ARB BOT
- Foco: arbitragem de preço + probabilidade implícita
- Agressivo, mas filtrado
- Recomendação CLARA (ENTRAR AGORA / EVITAR)
"""

import os
import time
import requests
from datetime import datetime

# ================== CONFIG ==================

POLY_ENDPOINT = os.getenv(
    "POLY_ENDPOINT",
    "https://polymarket.com/api/markets"
)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

INTERVAL = 900  # 15 minutos
MIN_LIQ = 20000
MIN_VOL = 30000
MIN_EDGE = 0.06      # 6% de edge mínimo
MAX_PRICE = 0.65     # evita comprar caro demais
COOLDOWN = 3600      # 1h por mercado

sent_cache = {}

# ================== TELEGRAM ==================

def send(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# ================== CORE ==================

def implied_prob(price):
    return round(price, 4)

def edge_score(market_prob, fair_prob):
    return round(fair_prob - market_prob, 4)

def cooldown_ok(mid):
    now = time.time()
    return now - sent_cache.get(mid, 0) > COOLDOWN

def fetch_markets():
    r = requests.get(POLY_ENDPOINT, timeout=20)
    r.raise_for_status()
    return r.json()

def analyze_market(m):
    try:
        yes_price = float(m["yesPrice"])
        no_price = float(m["noPrice"])
        volume = float(m["volume"])
        liq = float(m["liquidity"])
        title = m["title"]
        url = f"https://polymarket.com/market/{m['slug']}"
        mid = m["id"]
    except:
        return None

    if volume < MIN_VOL or liq < MIN_LIQ:
        return None

    # Fair probability heuristic
    # Quanto maior liquidez + volume, mais confiável
    fair_prob = round(
        (1 - no_price + yes_price) / 2, 4
    )

    market_prob = implied_prob(yes_price)
    edge = edge_score(market_prob, fair_prob)

    if edge < MIN_EDGE:
        return None

    if yes_price > MAX_PRICE:
        return None

    if not cooldown_ok(mid):
        return None

    return {
        "id": mid,
        "title": title,
        "yes": yes_price,
        "no": no_price,
        "edge": edge,
        "fair": fair_prob,
        "volume": volume,
        "liq": liq,
        "url": url
    }

# ================== LOOP ==================

def main():
    send("⚡ POLY EDGE BOT ONLINE\nModo: ARBITRAGEM DE PREÇO")

    while True:
        try:
            markets = fetch_markets()
            alerts = 0

            for m in markets:
                res = analyze_market(m)
                if not res:
                    continue

                sent_cache[res["id"]] = time.time()
                alerts += 1

                msg = (
                    "🚨 ARBITRAGEM DETECTADA\n\n"
                    f"🎯 AÇÃO: ENTRAR AGORA (YES)\n\n"
                    f"📌 {res['title']}\n\n"
                    f"💰 YES: {res['yes']}\n"
                    f"❌ NO: {res['no']}\n\n"
                    f"📊 Fair Prob: {res['fair']}\n"
                    f"⚖️ Edge: +{round(res['edge']*100,2)}%\n\n"
                    f"💧 Liquidez: {int(res['liq'])}\n"
                    f"📈 Volume: {int(res['volume'])}\n\n"
                    f"🔗 {res['url']}"
                )

                send(msg)

            if alerts == 0:
                print(f"[{datetime.utcnow()}] Nenhuma arb válida")

        except Exception as e:
            print("Erro:", e)

        time.sleep(INTERVAL)

# ================== START ==================

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID ausente")

    main()
