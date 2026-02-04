#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import traceback
import requests
from typing import Dict, Any, Optional

# =========================================================
# POLYMARKET AGENT-STYLE SIGNAL BOT (NO AUTO TRADING)
# - Market anomalies (price + volume)
# - News / context analysis
# - Human-readable reasoning
# - Telegram alerts only
# =========================================================

GAMMA_BASE = "https://gamma-api.polymarket.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "90"))
STATE_FILE = "state.json"

MIN_LIQUIDITY = 10000
MIN_VOLUME = 12000
MIN_PRICE_MOVE = 0.015
MIN_PCT_MOVE = 4
MIN_VOLUME_DELTA = 4000

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-agent-bot/1.0"})


# -------------------------
# Utilities
# -------------------------
def now():
    return int(time.time())


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def tg(msg):
    if not TELEGRAM_BOT_TOKEN:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    SESSION.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })


def http(path, params=None):
    r = SESSION.get(GAMMA_BASE + path, params=params or {}, timeout=20)
    r.raise_for_status()
    return r.json()


# -------------------------
# News Context (Agent Brain)
# -------------------------
def fetch_news_context(query: str) -> str:
    """
    Lightweight news context via Google News RSS.
    No API key needed.
    """
    try:
        url = "https://news.google.com/rss/search"
        params = {"q": query, "hl": "en", "gl": "US"}
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return "No recent headlines found."

        # ultra-simple parsing
        headlines = []
        for line in r.text.split("<title>")[2:6]:
            headline = line.split("</title>")[0]
            headlines.append("• " + headline)

        return "\n".join(headlines) if headlines else "No relevant news."
    except Exception:
        return "News lookup failed."


# -------------------------
# Market Logic
# -------------------------
def extract_yes_price(m):
    for k in ("outcomes", "tokens"):
        arr = m.get(k)
        if isinstance(arr, list):
            for o in arr:
                if (o.get("name") or "").lower() == "yes":
                    try:
                        return float(o.get("price"))
                    except Exception:
                        pass
    return None


def analyze_market(m, prev):
    price = extract_yes_price(m)
    if price is None:
        return None

    volume = float(m.get("volume", 0))
    liquidity = float(m.get("liquidity", 0))

    if liquidity < MIN_LIQUIDITY or volume < MIN_VOLUME:
        return None

    prev_price = prev.get("price", price)
    prev_volume = prev.get("volume", volume)

    d_price = price - prev_price
    d_pct = (d_price / prev_price * 100) if prev_price else 0
    d_vol = volume - prev_volume

    if abs(d_price) < MIN_PRICE_MOVE:
        return None
    if abs(d_pct) < MIN_PCT_MOVE:
        return None
    if d_vol < MIN_VOLUME_DELTA:
        return None

    confidence = "LOW"
    if abs(d_pct) > 8 and d_vol > 12000:
        confidence = "HIGH"
    elif abs(d_pct) > 5:
        confidence = "MEDIUM"

    title = m.get("question", "Polymarket Market")
    news = fetch_news_context(title[:80])

    return {
        "title": title,
        "price": price,
        "d_price": d_price,
        "d_pct": d_pct,
        "d_vol": d_vol,
        "confidence": confidence,
        "url": f"https://polymarket.com/market/{m.get('slug','')}",
        "news": news
    }


# -------------------------
# Main Loop
# -------------------------
def main():
    state = load_state()
    tg("🤖 Agent-style Polymarket bot online.\nWatching information flows, not just prices.")

    while True:
        try:
            markets = http("/markets", {"limit": 200})
            for m in markets:
                mid = str(m.get("id"))
                prev = state.get(mid, {})

                signal = analyze_market(m, prev)
                state[mid] = {
                    "price": extract_yes_price(m),
                    "volume": m.get("volume", 0),
                    "last": now()
                }

                if signal:
                    tg(
                        f"🧠 <b>AGENT SIGNAL ({signal['confidence']})</b>\n\n"
                        f"<b>{signal['title']}</b>\n\n"
                        f"YES price: <b>{signal['price']:.3f}</b>\n"
                        f"Δ price: <b>{signal['d_price']:+.3f}</b> ({signal['d_pct']:+.1f}%)\n"
                        f"Δ volume: <b>{int(signal['d_vol'])}</b>\n\n"
                        f"<b>Context / News</b>\n{signal['news']}\n\n"
                        f"Interpretation:\n"
                        f"• Market is repricing based on new information\n"
                        f"• Volume confirms this is not random\n"
                        f"• Confidence: <b>{signal['confidence']}</b>\n\n"
                        f"{signal['url']}"
                    )

            save_state(state)
            time.sleep(POLL_SECONDS)

        except Exception as e:
            tg(f"❌ Bot error:\n{str(e)[:200]}")
            time.sleep(30)


if __name__ == "__main__":
    main()
