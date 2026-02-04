# main.py
# Polymarket aggressive alerts bot (FIXED HTTP ERRORS)
# Uses OFFICIAL Gamma Markets API:
#   https://gamma-api.polymarket.com/markets
# Docs: https://docs.polymarket.com/developers/gamma-markets-api/get-markets
#
# Features:
# - More aggressive (more alerts)
# - Local history -> real deltas (volume + price) without relying on 1h fields
# - WATCH vs ACTION tiers
# - Retry/backoff for HTTP 429/5xx + graceful handling for 4xx
# - Anti-spam cooldown per market + resend only if improved

import os
import time
import math
import json
import traceback
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple

import requests

# ===================== CONFIG =====================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID env vars.")

# Scan cadence
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "45"))

# Delta window (how far back we compare)
WINDOW_SECONDS = int(os.getenv("WINDOW_SECONDS", str(60 * 60)))  # 1 hour

# Aggressive thresholds (lower => more alerts)
WATCH_VOL_DELTA = float(os.getenv("WATCH_VOL_DELTA", "80"))
WATCH_PRICE_PCT = float(os.getenv("WATCH_PRICE_PCT", "0.9"))
WATCH_LIQ = float(os.getenv("WATCH_LIQ", "5000"))

ACTION_VOL_DELTA = float(os.getenv("ACTION_VOL_DELTA", "350"))
ACTION_PRICE_PCT = float(os.getenv("ACTION_PRICE_PCT", "2.2"))
ACTION_LIQ = float(os.getenv("ACTION_LIQ", "15000"))

# Noise filters
MIN_YES_PRICE = float(os.getenv("MIN_YES_PRICE", "0.02"))
MAX_YES_PRICE = float(os.getenv("MAX_YES_PRICE", "0.98"))

# Anti-spam
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "15"))
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "8"))
IMPROVEMENT_RATIO = float(os.getenv("IMPROVEMENT_RATIO", "1.25"))

# Heartbeat (0 disables)
HEARTBEAT_EVERY_MIN = int(os.getenv("HEARTBEAT_EVERY_MIN", "60"))

# Local persistence
HISTORY_FILE = os.getenv("HISTORY_FILE", "pm_history.json")
MAX_SAMPLES_PER_MARKET = int(os.getenv("MAX_SAMPLES_PER_MARKET", "90"))

# Polymarket Gamma API (official)
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_PAGE_LIMIT = int(os.getenv("GAMMA_PAGE_LIMIT", "200"))  # per request
MAX_MARKETS_TOTAL = int(os.getenv("MAX_MARKETS_TOTAL", "400"))  # total per scan

# ===================== TELEGRAM =====================

def send_telegram(msg: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=20)
    # don't crash if telegram hiccups
    try:
        r.raise_for_status()
    except Exception:
        pass

# ===================== DATA MODEL =====================

@dataclass
class Signal:
    market_id: str
    title: str
    url: str
    yes_price: float
    vol_delta: float
    price_pct: float
    liquidity: float
    direction: str  # "YES_UP" / "YES_DOWN"
    score: float
    tier: str       # "WATCH" / "ACTION"

# ===================== STATE =====================

history: Dict[str, List[Dict[str, float]]] = {}     # market_id -> [{t, vol, yes}, ...]
last_alert: Dict[str, Dict[str, float]] = {}        # market_id -> {ts, score}
last_heartbeat_ts = 0.0

# ===================== HELPERS =====================

def load_state() -> None:
    global history, last_alert
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        history = data.get("history", {}) or {}
        last_alert = data.get("last_alert", {}) or {}
        if not isinstance(history, dict): history = {}
        if not isinstance(last_alert, dict): last_alert = {}
    except Exception:
        history, last_alert = {}, {}

def save_state() -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"history": history, "last_alert": last_alert}, f)
    except Exception:
        pass

def score_signal(vol_delta: float, price_pct: float, liquidity: float) -> float:
    v = math.log10(max(vol_delta, 1.0))
    p = price_pct / 2.0
    l = math.log10(max(liquidity, 1.0)) / 2.0
    return v * 1.2 + p * 2.0 + l * 1.0

def classify(vol_delta: float, price_pct: float, liquidity: float) -> Optional[str]:
    if vol_delta >= ACTION_VOL_DELTA and price_pct >= ACTION_PRICE_PCT and liquidity >= ACTION_LIQ:
        return "ACTION"
    if vol_delta >= WATCH_VOL_DELTA and price_pct >= WATCH_PRICE_PCT and liquidity >= WATCH_LIQ:
        return "WATCH"
    return None

def should_alert(market_id: str, score: float, now: float) -> bool:
    prev = last_alert.get(market_id)
    if not prev:
        return True
    age_min = (now - float(prev.get("ts", 0.0))) / 60.0
    prev_score = float(prev.get("score", 0.0))
    if age_min < COOLDOWN_MINUTES:
        return score >= prev_score * IMPROVEMENT_RATIO
    return True

def prune_and_add_sample(market_id: str, now: float, vol: float, yes: float) -> None:
    samples = history.get(market_id, [])
    samples.append({"t": now, "vol": vol, "yes": yes})

    # prune by time (keep a little more than window)
    cutoff = now - (WINDOW_SECONDS * 1.3)
    samples = [s for s in samples if s.get("t", 0) >= cutoff]

    # prune by count
    if len(samples) > MAX_SAMPLES_PER_MARKET:
        samples = samples[-MAX_SAMPLES_PER_MARKET:]

    history[market_id] = samples

def get_baseline_sample(market_id: str, now: float) -> Optional[Dict[str, float]]:
    samples = history.get(market_id, [])
    if not samples:
        return None
    target = now - WINDOW_SECONDS

    # pick sample closest to target time
    best = samples[0]
    best_dist = abs(best.get("t", 0) - target)
    for s in samples[1:]:
        dist = abs(s.get("t", 0) - target)
        if dist < best_dist:
            best, best_dist = s, dist
    return best

def parse_yes_price(m: Dict[str, Any]) -> Optional[float]:
    """
    Gamma API commonly gives:
      - outcomePrices: string (often JSON-like) OR list
      - outcomes: string (e.g., '["Yes","No"]')
    We'll extract YES as first entry.
    """
    op = m.get("outcomePrices")
    if op is None:
        return None

    # If already list
    if isinstance(op, list) and len(op) >= 1:
        try:
            return float(op[0])
        except Exception:
            return None

    # If it's a string, it might be like '["0.53","0.47"]' or '[0.53,0.47]'
    if isinstance(op, str):
        s = op.strip()
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list) and len(parsed) >= 1:
                return float(parsed[0])
        except Exception:
            # fallback: strip brackets and split
            try:
                s2 = s.strip("[]")
                first = s2.split(",")[0].strip().strip('"').strip("'")
                return float(first)
            except Exception:
                return None

    return None

def build_market_url(slug: Optional[str]) -> str:
    if not slug:
        return "https://polymarket.com"
    return f"https://polymarket.com/market/{slug}"

# ===================== HTTP (ROBUST) =====================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; PolymarketAlertBot/1.0)",
    "Accept": "application/json,text/plain,*/*",
})

def get_json_with_retries(url: str, params: Dict[str, Any], retries: int = 5) -> Any:
    backoff = 1.2
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=25)

            # Rate limit / temporary errors
            if r.status_code in (429, 500, 502, 503, 504):
                sleep_s = backoff ** attempt
                time.sleep(sleep_s)
                continue

            # Other client errors -> stop retrying
            r.raise_for_status()
            return r.json()

        except requests.HTTPError as e:
            # Non-retryable 4xx typically
            raise
        except Exception:
            # Network hiccup -> retry
            sleep_s = backoff ** attempt
            time.sleep(sleep_s)

    raise RuntimeError("HTTP error: retries exhausted")

# ===================== FETCH MARKETS =====================

def fetch_markets_gamma() -> List[Dict[str, Any]]:
    """
    Fetch active + not closed markets from Gamma API with pagination.
    """
    all_markets: List[Dict[str, Any]] = []
    offset = 0

    while len(all_markets) < MAX_MARKETS_TOTAL:
        params = {
            "active": "true",
            "closed": "false",
            "limit": GAMMA_PAGE_LIMIT,
            "offset": offset,
            # Helpful ordering: higher recent volume tends to be more relevant
            "order": "volume24hr",
            "ascending": "false",
        }

        batch = get_json_with_retries(GAMMA_MARKETS_URL, params=params)

        if not isinstance(batch, list) or len(batch) == 0:
            break

        all_markets.extend(batch)
        offset += len(batch)

        # If we got less than limit, no more pages
        if len(batch) < GAMMA_PAGE_LIMIT:
            break

    return all_markets[:MAX_MARKETS_TOTAL]

# ===================== SIGNALS =====================

def build_signals(markets: List[Dict[str, Any]], now: float) -> List[Signal]:
    signals: List[Signal] = []

    for m in markets:
        try:
            market_id = str(m.get("id", "")).strip()
            if not market_id:
                continue

            title = (m.get("question") or "").strip() or "Untitled market"
            slug = m.get("slug")
            url = build_market_url(slug)

            yes_price = parse_yes_price(m)
            if yes_price is None:
                continue

            # Gamma provides numeric fields too
            liquidity = float(m.get("liquidityNum") or m.get("liquidity_num") or 0.0)
            volume = float(m.get("volumeNum") or m.get("volume_num") or 0.0)

            if yes_price < MIN_YES_PRICE or yes_price > MAX_YES_PRICE:
                continue

            # Update local history
            prune_and_add_sample(market_id, now, volume, yes_price)
            base = get_baseline_sample(market_id, now)
            if not base:
                continue

            vol_delta = max(0.0, volume - float(base.get("vol", volume)))
            price_abs = yes_price - float(base.get("yes", yes_price))
            base_yes = float(base.get("yes", yes_price)) or yes_price
            price_pct = (abs(price_abs) / base_yes * 100.0) if base_yes > 0 else 0.0

            tier = classify(vol_delta, price_pct, liquidity)
            if not tier:
                continue

            direction = "YES_UP" if price_abs >= 0 else "YES_DOWN"
            score = score_signal(vol_delta, price_pct, liquidity)

            if not should_alert(market_id, score, now):
                continue

            signals.append(
                Signal(
                    market_id=market_id,
                    title=title,
                    url=url,
                    yes_price=yes_price,
                    vol_delta=vol_delta,
                    price_pct=price_pct,
                    liquidity=liquidity,
                    direction=direction,
                    score=score,
                    tier=tier,
                )
            )

        except Exception:
            continue

    # ACTION first, then by score
    signals.sort(key=lambda s: (s.tier == "ACTION", s.score), reverse=True)
    return signals

def format_signal(s: Signal) -> str:
    arrow = "⬆️" if s.direction == "YES_UP" else "⬇️"
    momentum_line = "Suggested (momentum): YES" if s.direction == "YES_UP" else "Suggested (momentum): NO / avoid YES"
    rec_line = "✅ ACTION: consider entry now" if s.tier == "ACTION" else "👀 WATCH: monitor (possible entry)"

    return (
        f"🚨 {s.tier} {arrow}\n"
        f"🎯 {rec_line}\n"
        f"{momentum_line}\n"
        f"🧠 Reason: VolΔ={int(s.vol_delta)} | PriceMove={s.price_pct:.2f}% | Liq={int(s.liquidity)} | Score={s.score:.2f}\n"
        f"📝 {s.title}\n"
        f"{s.url}"
    )

# ===================== MAIN LOOP =====================

def main() -> None:
    global last_heartbeat_ts
    load_state()
    send_telegram("🤖 Bot ONLINE (aggressive) — using Gamma API + retries.")

    while True:
        now = time.time()
        try:
            markets = fetch_markets_gamma()
            signals = build_signals(markets, now)

            sent = 0
            for s in signals:
                if sent >= MAX_ALERTS_PER_SCAN:
                    break

                send_telegram(format_signal(s))
                last_alert[s.market_id] = {"ts": now, "score": s.score}
                sent += 1

            # Heartbeat (optional)
            if HEARTBEAT_EVERY_MIN > 0:
                if (now - last_heartbeat_ts) / 60.0 >= HEARTBEAT_EVERY_MIN:
                    send_telegram(f"🟢 Scan OK. Markets fetched: {len(markets)} | Signals: {len(signals)} | Sent: {sent}")
                    last_heartbeat_ts = now

            save_state()

        except Exception as e:
            # Avoid spamming long traces in Telegram
            send_telegram(f"⚠️ Bot error (HTTP or parse): {type(e).__name__}: {str(e)[:160]}")
            traceback.print_exc()

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
