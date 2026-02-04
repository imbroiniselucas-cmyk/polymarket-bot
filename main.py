# main.py
# More aggressive Polymarket opportunities bot (more alerts + clearer recommendations)
# - Lower thresholds (volume/price/liquidity) to catch more mid-size moves
# - Two-tier alerts: WATCH (softer) vs ACTION (stronger)
# - Anti-spam: cooldown per market + only resend if signal improved
# - Clear recommendation text (BUY YES / BUY NO / WATCH) + reason
#
# Assumes you already have:
# - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID set (env vars)
# - Your existing market fetch function (replace fetch_markets()) or Polymarket endpoint code
#
# If you previously used "telebot", keep it. If not, you can swap send_telegram() to requests.

import os
import time
import math
import json
import traceback
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple

import requests

# =============== CONFIG (AGGRESSIVE) ===============

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))  # scan every 60s (more frequent = more alerts)
MAX_MARKETS_PER_SCAN = int(os.getenv("MAX_MARKETS_PER_SCAN", "250"))

# Aggressive thresholds (lower = more alerts)
WATCH_VOL_DELTA = int(os.getenv("WATCH_VOL_DELTA", "150"))          # was likely 500-2000
WATCH_PRICE_PCT = float(os.getenv("WATCH_PRICE_PCT", "1.2"))        # % move
WATCH_LIQ = int(os.getenv("WATCH_LIQ", "8000"))

ACTION_VOL_DELTA = int(os.getenv("ACTION_VOL_DELTA", "600"))
ACTION_PRICE_PCT = float(os.getenv("ACTION_PRICE_PCT", "3.0"))
ACTION_LIQ = int(os.getenv("ACTION_LIQ", "20000"))

# Filtering / hygiene
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.02"))   # ignore ultra-tiny prices (noise)
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.98"))   # ignore near-certain (noise)
MIN_MARKET_AGE_HOURS = float(os.getenv("MIN_MARKET_AGE_HOURS", "0"))  # set >0 if you want to ignore brand-new markets

# Anti-spam controls
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "25"))  # per market cooldown
IMPROVEMENT_RATIO_TO_RESEND = float(os.getenv("IMPROVEMENT_RATIO_TO_RESEND", "1.25"))  # resend only if score improves 25%
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "5"))  # cap per scan

# Optional: 1 message/hour to show it's alive
HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_EVERY_MIN", "60"))

# If your bot already has Polymarket API integration, keep it.
# Otherwise, you'll need to implement fetch_markets() properly.
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()  # optional if you already have your own client

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars.")


# =============== TELEGRAM ===============

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


# =============== DATA MODEL ===============

@dataclass
class MarketSignal:
    market_id: str
    title: str
    url: str
    yes_price: float
    no_price: float
    liq: float
    vol: float
    vol_delta: float
    price_pct: float
    price_abs: float
    direction: str  # "YES_UP" / "YES_DOWN"
    score: float
    tier: str       # "WATCH" or "ACTION"


# =============== HELPERS ===============

def clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))

def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default

def score_signal(vol_delta: float, price_pct: float, liq: float) -> float:
    """
    A simple score: log-scaled volume + price move + liquidity
    Higher => stronger signal
    """
    v = math.log10(max(vol_delta, 1.0))  # 0..~4
    p = clamp(price_pct / 5.0, 0.0, 3.0) # 0..3
    l = clamp(math.log10(max(liq, 1.0)) / 2.0, 0.0, 3.5)  # 0..~3.5
    return (v * 1.2) + (p * 2.0) + (l * 1.0)

def classify_tier(vol_delta: float, price_pct: float, liq: float) -> Optional[str]:
    watch = (vol_delta >= WATCH_VOL_DELTA and price_pct >= WATCH_PRICE_PCT and liq >= WATCH_LIQ)
    action = (vol_delta >= ACTION_VOL_DELTA and price_pct >= ACTION_PRICE_PCT and liq >= ACTION_LIQ)
    if action:
        return "ACTION"
    if watch:
        return "WATCH"
    return None

def recommendation_text(direction: str, tier: str) -> str:
    if tier == "ACTION":
        return "✅ ACTION: consider entry now"
    return "👀 WATCH: monitor (possible entry)"

def side_text(direction: str) -> str:
    # direction describes what happened to YES
    # If YES price jumps quickly, often late but still informative.
    # We'll phrase clearly: buy YES vs buy NO is a choice; here we mainly indicate momentum.
    if direction == "YES_UP":
        return "Momentum: YES moving up (market getting more confident)."
    return "Momentum: YES moving down (market getting less confident)."


# =============== FETCH MARKETS (REPLACE IF NEEDED) ===============

def fetch_markets() -> List[Dict[str, Any]]:
    """
    You MUST adapt this to your existing Polymarket fetching code.
    This stub tries an optional endpoint if you set POLY_ENDPOINT,
    otherwise it raises.

    Expected each market dict should include at least:
      id, title, url, yes_price, no_price, liquidity, volume, volume_1h_ago, yes_price_1h_ago
    """
    if not POLY_ENDPOINT:
        raise RuntimeError(
            "fetch_markets() is a stub. Plug in your existing Polymarket market fetcher, "
            "or set POLY_ENDPOINT to a JSON endpoint returning market data."
        )

    r = requests.get(POLY_ENDPOINT, timeout=25)
    r.raise_for_status()
    data = r.json()
    # If endpoint returns a wrapper, adapt here.
    if isinstance(data, dict) and "markets" in data:
        return data["markets"][:MAX_MARKETS_PER_SCAN]
    if isinstance(data, list):
        return data[:MAX_MARKETS_PER_SCAN]
    raise RuntimeError("POLY_ENDPOINT returned unexpected JSON shape.")


# =============== STATE (anti-spam) ===============

# last_alert[market_id] = {"ts": epoch_seconds, "score": float}
last_alert: Dict[str, Dict[str, float]] = {}
last_heartbeat_ts: float = 0.0


def should_alert(market_id: str, score: float, now: float) -> bool:
    info = last_alert.get(market_id)
    if not info:
        return True
    age_min = (now - info["ts"]) / 60.0
    if age_min < COOLDOWN_MINUTES:
        # allow resend if improved a lot
        if score >= info["score"] * IMPROVEMENT_RATIO_TO_RESEND:
            return True
        return False
    return True

def remember_alert(market_id: str, score: float, now: float) -> None:
    last_alert[market_id] = {"ts": now, "score": score}


# =============== SIGNAL BUILDING ===============

def build_signals(markets: List[Dict[str, Any]]) -> List[MarketSignal]:
    signals: List[MarketSignal] = []
    for m in markets:
        market_id = str(m.get("id") or m.get("market_id") or "")
        if not market_id:
            continue

        title = str(m.get("title") or m.get("question") or "Untitled market")
        url = str(m.get("url") or m.get("market_url") or "")
        yes_price = safe_float(m.get("yes_price"))
        no_price = safe_float(m.get("no_price"), default=(1.0 - yes_price if yes_price else 0.0))
        liq = safe_float(m.get("liquidity") or m.get("liq"))
        vol = safe_float(m.get("volume") or m.get("vol"))
        vol_prev = safe_float(m.get("volume_1h_ago") or m.get("vol_1h_ago") or m.get("volume_prev"), default=vol)
        yes_prev = safe_float(m.get("yes_price_1h_ago") or m.get("yes_prev") or m.get("yes_price_prev"), default=yes_price)

        # Basic hygiene filters
        if yes_price < MIN_PRICE or yes_price > MAX_PRICE:
            continue

        vol_delta = max(0.0, vol - vol_prev)
        price_abs = yes_price - yes_prev
        if yes_prev > 0:
            price_pct = abs(price_abs) / yes_prev * 100.0
        else:
            price_pct = 0.0

        tier = classify_tier(vol_delta, price_pct, liq)
        if not tier:
            continue

        direction = "YES_UP" if price_abs >= 0 else "YES_DOWN"
        score = score_signal(vol_delta, price_pct, liq)

        signals.append(
            MarketSignal(
                market_id=market_id,
                title=title,
                url=url,
                yes_price=yes_price,
                no_price=no_price,
                liq=liq,
                vol=vol,
                vol_delta=vol_delta,
                price_pct=price_pct,
                price_abs=price_abs,
                direction=direction,
                score=score,
                tier=tier,
            )
        )

    # Sort strongest first
    signals.sort(key=lambda s: (s.tier == "ACTION", s.score), reverse=True)
    return signals


# =============== FORMATTING ===============

def format_alert(s: MarketSignal) -> str:
    arrow = "⬆️" if s.direction == "YES_UP" else "⬇️"
    rec = recommendation_text(s.direction, s.tier)
    side = side_text(s.direction)

    # Clear suggestion wording (not just YES/NO)
    # We avoid pretending certainty; we describe momentum and what to watch.
    if s.direction == "YES_UP":
        action_line = "Suggested side (momentum): **YES** (market confidence increasing)."
    else:
        action_line = "Suggested side (momentum): **NO** / avoid YES (market confidence decreasing)."

    # Telegram doesn't render markdown by default unless parse_mode set;
    # keep it plain text.
    msg = (
        f"🚨 {s.tier} | Momentum {arrow}\n"
        f"🎯 {rec}\n"
        f"{action_line}\n"
        f"🧠 Reason: VolΔ={int(s.vol_delta)} | PriceMove={s.price_pct:.2f}% | Liq={int(s.liq)} | Score={s.score:.2f}\n"
        f"📌 {side}\n"
        f"📝 {s.title}\n"
        f"{s.url}".strip()
    )
    return msg


# =============== MAIN LOOP ===============

def main() -> None:
    global last_heartbeat_ts
    send_telegram("🤖 Bot online (AGGRESSIVE): more alerts + WATCH/ACTION tiers + anti-spam.")

    while True:
        now = time.time()
        try:
            markets = fetch_markets()
            signals = build_signals(markets)

            sent = 0
            for s in signals:
                if sent >= MAX_ALERTS_PER_SCAN:
                    break

                if should_alert(s.market_id, s.score, now):
                    send_telegram(format_alert(s))
                    remember_alert(s.market_id, s.score, now)
                    sent += 1

            # Heartbeat (optional, low noise)
            if HEARTBEAT_EVERY_MIN > 0:
                if (now - last_heartbeat_ts) / 60.0 >= HEARTBEAT_EVERY_MIN:
                    send_telegram(f"🟢 Scan ok. Signals this scan: {len(signals)} (sent {sent}).")
                    last_heartbeat_ts = now

        except Exception as e:
            # Only send a short error (avoid spam)
            err = f"⚠️ Bot error: {type(e).__name__}: {str(e)[:180]}"
            try:
                send_telegram(err)
            except Exception:
                pass

            # Also log locally if you have logs
            traceback.print_exc()

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
