# main.py
# Polymarket aggressive alerts bot (WORKING VERSION)
# - Fetches markets directly from Polymarket
# - Stores local history to compute real deltas (price + volume)
# - More aggressive thresholds = more alerts
# - WATCH vs ACTION tiers
# - Anti-spam cooldown
# - Clear recommendations (YES / NO momentum)

import os
import time
import math
import json
import traceback
from dataclasses import dataclass
from typing import Dict, Any, List, Optional
import requests

# ===================== CONFIG =====================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

POLL_SECONDS = 45
WINDOW_SECONDS = 60 * 60  # 1h window for deltas

# Aggressive thresholds
WATCH_VOL_DELTA = 80
WATCH_PRICE_PCT = 0.9
WATCH_LIQ = 5_000

ACTION_VOL_DELTA = 350
ACTION_PRICE_PCT = 2.2
ACTION_LIQ = 15_000

MIN_YES_PRICE = 0.02
MAX_YES_PRICE = 0.98

COOLDOWN_MINUTES = 15
MAX_ALERTS_PER_SCAN = 8
IMPROVEMENT_RATIO = 1.25

HEARTBEAT_EVERY_MIN = 60

HISTORY_FILE = "pm_history.json"
MAX_SAMPLES_PER_MARKET = 60

POLYMARKET_URL = "https://polymarket.com/api/markets"

# ===================== TELEGRAM =====================

def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }
    requests.post(url, json=payload, timeout=20)

# ===================== DATA =====================

@dataclass
class Signal:
    market_id: str
    title: str
    url: str
    yes_price: float
    vol_delta: float
    price_pct: float
    liquidity: float
    direction: str
    score: float
    tier: str

# ===================== STATE =====================

history: Dict[str, List[Dict[str, float]]] = {}
last_alert: Dict[str, Dict[str, float]] = {}
last_heartbeat = 0.0

# ===================== HELPERS =====================

def load_state():
    global history, last_alert
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
            history = data.get("history", {})
            last_alert = data.get("last_alert", {})
    except Exception:
        history = {}
        last_alert = {}

def save_state():
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump({"history": history, "last_alert": last_alert}, f)
    except Exception:
        pass

def score_signal(vol_delta, price_pct, liquidity):
    v = math.log10(max(vol_delta, 1))
    p = price_pct / 2
    l = math.log10(max(liquidity, 1)) / 2
    return v * 1.2 + p * 2 + l

def classify(vol_delta, price_pct, liquidity):
    if vol_delta >= ACTION_VOL_DELTA and price_pct >= ACTION_PRICE_PCT and liquidity >= ACTION_LIQ:
        return "ACTION"
    if vol_delta >= WATCH_VOL_DELTA and price_pct >= WATCH_PRICE_PCT and liquidity >= WATCH_LIQ:
        return "WATCH"
    return None

def should_alert(mid, score, now):
    prev = last_alert.get(mid)
    if not prev:
        return True
    age = (now - prev["ts"]) / 60
    if age < COOLDOWN_MINUTES:
        return score >= prev["score"] * IMPROVEMENT_RATIO
    return True

# ===================== FETCH =====================

def fetch_markets():
    r = requests.get(POLYMARKET_URL, params={"active": "true"}, timeout=20)
    r.raise_for_status()
    return r.json()

# ===================== MAIN =====================

def main():
    global last_heartbeat
    load_state()
    send_telegram("🤖 Polymarket bot ONLINE (aggressive mode)")

    while True:
        now = time.time()
        sent = 0

        try:
            markets = fetch_markets()

            for m in markets:
                if sent >= MAX_ALERTS_PER_SCAN:
                    break

                try:
                    market_id = m["id"]
                    title = m["question"]
                    slug = m["slug"]
                    url = f"https://polymarket.com/market/{slug}"

                    outcomes = m.get("outcomes", [])
                    if len(outcomes) < 2:
                        continue

                    yes_price = float(outcomes[0]["price"])
                    liquidity = float(m.get("liquidity", 0))
                    volume = float(m.get("volume", 0))

                    if yes_price < MIN_YES_PRICE or yes_price > MAX_YES_PRICE:
                        continue

                    samples = history.get(market_id, [])
                    samples.append({"t": now, "vol": volume, "yes": yes_price})
                    samples = samples[-MAX_SAMPLES_PER_MARKET:]
                    history[market_id] = samples

                    base = samples[0]
                    vol_delta = volume - base["vol"]
                    price_abs = yes_price - base["yes"]
                    price_pct = abs(price_abs) / base["yes"] * 100 if base["yes"] > 0 else 0

                    tier = classify(vol_delta, price_pct, liquidity)
                    if not tier:
                        continue

                    direction = "YES ↑" if price_abs >= 0 else "YES ↓"
                    score = score_signal(vol_delta, price_pct, liquidity)

                    if not should_alert(market_id, score, now):
                        continue

                    msg = (
                        f"🚨 {tier}\n"
                        f"🎯 Momentum: {direction}\n"
                        f"📈 Price move: {price_pct:.2f}%\n"
                        f"💰 VolΔ: {int(vol_delta)} | Liq: {int(liquidity)}\n"
                        f"🧠 Score: {score:.2f}\n"
                        f"📝 {title}\n"
                        f"{url}"
                    )

                    send_telegram(msg)
                    last_alert[market_id] = {"ts": now, "score": score}
                    sent += 1

                except Exception:
                    continue

            if HEARTBEAT_EVERY_MIN > 0:
                if (now - last_heartbeat) / 60 >= HEARTBEAT_EVERY_MIN:
                    send_telegram("🟢 Bot ativo. Scan concluído.")
                    last_heartbeat = now

            save_state()

        except Exception as e:
            send_telegram(f"⚠️ Bot error: {type(e).__name__}")
            traceback.print_exc()

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
