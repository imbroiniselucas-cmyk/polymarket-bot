#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import requests
from datetime import datetime

# =========================
# ENV / CONFIG
# =========================

POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "https://polymarket.com/api/markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

INTERVAL = int(os.getenv("INTERVAL_SEC", "600"))   # 10 min default
TOP_N = int(os.getenv("TOP_N", "7"))

MIN_LIQ = float(os.getenv("MIN_LIQ", "20000"))
MIN_VOL = float(os.getenv("MIN_VOL", "25000"))

ARB_CUSHION = float(os.getenv("ARB_CUSHION", "0.010"))  # 1%
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.14"))

MIN_PRICE = 0.02
MAX_PRICE = 0.98

BASE_MIN_EDGE = float(os.getenv("BASE_MIN_EDGE", "0.035"))
MAX_ENTRY_YES = float(os.getenv("MAX_ENTRY_YES", "0.72"))

COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "1800"))
REQUIRE_2_CYCLES = os.getenv("REQUIRE_2_CYCLES", "0") == "1"

# =========================
# STATE
# =========================

sent_cache = {}
seen_once = set()

# =========================
# TELEGRAM
# =========================

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

# =========================
# HELPERS
# =========================

def cooldown_ok(k):
    return time.time() - sent_cache.get(k, 0) > COOLDOWN_SEC

def clamp(x, a, b):
    return max(a, min(b, x))

def score(edge, liq, vol, spread):
    e = clamp(edge / 0.12, 0, 1)
    l = clamp(math.log10(max(liq, 1)) / 6, 0, 1)
    v = clamp(math.log10(max(vol, 1)) / 6, 0, 1)
    s = clamp(spread / MAX_SPREAD, 0, 1)
    return round(clamp((0.55*e + 0.25*l + 0.25*v - 0.25*s), 0, 1) * 10, 2)

def dyn_min_edge(liq, vol):
    boost = 0
    if liq > 250000: boost += 0.01
    if vol > 500000: boost += 0.01
    return max(0.03, BASE_MIN_EDGE - boost)

def fetch():
    r = requests.get(POLY_ENDPOINT, timeout=20)
    r.raise_for_status()
    return r.json()

# =========================
# PARSE
# =========================

def parse(m):
    try:
        yes = float(m["yesPrice"])
        no = float(m["noPrice"])
        liq = float(m.get("liquidity", 0))
        vol = float(m.get("volume", 0))
        title = m.get("title", "Untitled")
        mid = str(m.get("id"))
        slug = m.get("slug", "")
    except:
        return None

    if not (MIN_PRICE <= yes <= MAX_PRICE and MIN_PRICE <= no <= MAX_PRICE):
        return None

    spread = abs((1 - no) - yes)

    return {
        "id": mid,
        "title": title,
        "yes": yes,
        "no": no,
        "liq": liq,
        "vol": vol,
        "spread": spread,
        "url": f"https://polymarket.com/market/{slug}" if slug else ""
    }

# =========================
# DETECTORS
# =========================

def classic_arb(x):
    s = x["yes"] + x["no"]
    gap = 1 - s
    if gap <= ARB_CUSHION: return None
    if x["liq"] < MIN_LIQ or x["vol"] < MIN_VOL: return None
    if x["spread"] > MAX_SPREAD: return None

    key = x["id"] + "|ARB"
    if REQUIRE_2_CYCLES and key not in seen_once:
        seen_once.add(key); return None
    if not cooldown_ok(key): return None

    sc = score(gap, x["liq"], x["vol"], x["spread"])
    return ("ARB", key, gap, sc, x)

def mispricing_yes(x):
    if x["liq"] < MIN_LIQ or x["vol"] < MIN_VOL: return None
    if x["spread"] > MAX_SPREAD: return None
    if x["yes"] > MAX_ENTRY_YES: return None

    fair = (x["yes"] + (1 - x["no"])) / 2
    edge = fair - x["yes"]
    if edge < dyn_min_edge(x["liq"], x["vol"]): return None

    key = x["id"] + "|YES"
    if REQUIRE_2_CYCLES and key not in seen_once:
        seen_once.add(key); return None
    if not cooldown_ok(key): return None

    sc = score(edge, x["liq"], x["vol"], x["spread"])
    return ("YES", key, edge, sc, x)

# =========================
# MAIN LOOP
# =========================

def main():
    send("⚡ POLY EDGE BOT ONLINE\nModo: ARB + MISPRICING")

    while True:
        try:
            raw = fetch()
            alerts = []

            for m in raw:
                x = parse(m)
                if not x: continue

                a = classic_arb(x)
                if a: alerts.append(a)

                y = mispricing_yes(x)
                if y: alerts.append(y)

            alerts.sort(key=lambda z: z[3], reverse=True)

            for t, key, edge, sc, x in alerts[:TOP_N]:
                sent_cache[key] = time.time()

                if t == "ARB":
                    msg = (
                        "🚨 ARBITRAGEM\n"
                        "🎯 AÇÃO: ARB (YES + NO)\n\n"
                        f"{x['title']}\n\n"
                        f"YES: {x['yes']:.3f} | NO: {x['no']:.3f}\n"
                        f"GAP: {round(edge*100,2)}%\n"
                        f"Score: {sc}/10\n\n"
                        f"{x['url']}"
                    )
                else:
                    msg = (
                        "🚨 MISPRICING\n"
                        "🎯 AÇÃO: ENTRAR YES\n\n"
                        f"{x['title']}\n\n"
                        f"YES: {x['yes']:.3f}\n"
                        f"EDGE: +{round(edge*100,2)}%\n"
                        f"Score: {sc}/10\n\n"
                        f"{x['url']}"
                    )

                send(msg)

            if not alerts:
                print(f"[{datetime.utcnow()}] sem oportunidades")

        except Exception as e:
            print("Erro:", e)

        time.sleep(INTERVAL)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM TOKEN / CHAT ID ausente")
    main()
