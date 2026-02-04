#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import traceback
from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
import requests

# -----------------------------
# CONFIG
# -----------------------------
GAMMA_BASE = "https://gamma-api.polymarket.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))  # 60s default

STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Market filtering (avoid scanning thousands every minute)
MARKET_LIMIT_PER_POLL = int(os.getenv("MARKET_LIMIT_PER_POLL", "200"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "15000"))         # USD
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "20000"))       # USD (approx using "volume" field)
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.95"))                  # ignore near-certain
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.02"))                  # ignore ultra tiny

# Signal thresholds (tune if you're not seeing alerts)
MIN_ABS_PRICE_MOVE = float(os.getenv("MIN_ABS_PRICE_MOVE", "0.02"))   # 2 cents
MIN_PCT_PRICE_MOVE = float(os.getenv("MIN_PCT_PRICE_MOVE", "6.0"))    # 6%
MIN_VOLUME_DELTA = float(os.getenv("MIN_VOLUME_DELTA", "8000"))       # +$8k since last check

# Anti-spam cooldown per market
ALERT_COOLDOWN_MIN = int(os.getenv("ALERT_COOLDOWN_MIN", "120"))  # 2h

# Optional keyword include/exclude
INCLUDE_KEYWORDS = [k.strip().lower() for k in os.getenv("INCLUDE_KEYWORDS", "").split(",") if k.strip()]
EXCLUDE_KEYWORDS = [k.strip().lower() for k in os.getenv("EXCLUDE_KEYWORDS", "").split(",") if k.strip()]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "pm-opportunity-bot/1.0"})


# -----------------------------
# UTIL
# -----------------------------
def now_ts() -> int:
    return int(time.time())

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"markets": {}, "last_run": 0}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"markets": {}, "last_run": 0}

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def tg_send(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. Printing instead:\n")
        print(msg)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True,
        "parse_mode": "HTML"
    }
    r = SESSION.post(url, json=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:200]}")

def http_get(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    url = f"{GAMMA_BASE}{path}"
    r = SESSION.get(url, params=params or {}, timeout=25)
    r.raise_for_status()
    return r.json()

def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def pct_change(old: float, new: float) -> float:
    if old <= 0:
        return 0.0
    return (new - old) / old * 100.0

def clip(s: str, n: int = 170) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def matches_keywords(text: str) -> bool:
    t = (text or "").lower()
    if INCLUDE_KEYWORDS and not any(k in t for k in INCLUDE_KEYWORDS):
        return False
    if EXCLUDE_KEYWORDS and any(k in t for k in EXCLUDE_KEYWORDS):
        return False
    return True


# -----------------------------
# MCP-LIKE FUNCTIONS (via Gamma API)
# -----------------------------
def list_markets(limit: int = 100, offset: int = 0, status: str = "open") -> List[Dict[str, Any]]:
    # Gamma has "markets" endpoint. Fields vary; we use robust access.
    # Note: status filtering isn't always perfect on their side; we filter again below.
    params = {"limit": limit, "offset": offset}
    data = http_get("/markets", params=params)
    if isinstance(data, list):
        markets = data
    else:
        markets = data.get("markets", []) if isinstance(data, dict) else []
    # Filter open
    out = []
    for m in markets:
        st = (m.get("status") or "").lower()
        if status == "open":
            if st and st != "open":
                continue
        out.append(m)
    return out

def market_url(m: Dict[str, Any]) -> str:
    # Try to build a shareable link
    # Some objects have "slug". If not, fallback to "conditionId"/"id"
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = m.get("id") or m.get("conditionId") or ""
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

def extract_best_yes_price(m: Dict[str, Any]) -> Optional[float]:
    """
    Many Gamma responses include a 'outcomes' list or 'tokens' with prices.
    We try multiple shapes safely.
    Returns a single representative probability/price (0..1) if possible.
    """
    # Common shapes:
    # - m["outcomes"] = [{"name":"Yes","price":"0.23"}, ...]
    # - m["tokens"] = [{"outcome":"Yes","price":"0.23"}, ...]
    # - m["bestAsk"]/["bestBid"] etc (varies)
    for key in ("outcomes", "tokens"):
        arr = m.get(key)
        if isinstance(arr, list):
            for o in arr:
                name = (o.get("name") or o.get("outcome") or "").lower()
                if name == "yes":
                    p = o.get("price") or o.get("probability") or o.get("lastPrice")
                    p = safe_float(p, default=float("nan"))
                    if not math.isnan(p):
                        return p
    # fallback single fields
    for key in ("price", "probability", "lastPrice"):
        p = safe_float(m.get(key), default=float("nan"))
        if not math.isnan(p):
            return p
    return None


# -----------------------------
# SIGNAL LOGIC
# -----------------------------
@dataclass
class Signal:
    market_id: str
    title: str
    url: str
    yes_price: float
    liquidity: float
    volume: float
    d_price_abs: float
    d_price_pct: float
    d_volume: float
    reason: str
    severity: str  # "WATCH" | "STRONG"

def compute_signal(m: Dict[str, Any], prev: Dict[str, Any]) -> Optional[Signal]:
    mid = str(m.get("id") or m.get("conditionId") or "")
    title = m.get("question") or m.get("title") or m.get("name") or "Polymarket market"
    if not matches_keywords(title):
        return None

    liquidity = safe_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidityUSD"))
    volume = safe_float(m.get("volume") or m.get("volumeNum") or m.get("volumeUSD") or m.get("volume24h"))
    yes_price = extract_best_yes_price(m)
    if yes_price is None:
        return None

    # Basic filters
    if liquidity < MIN_LIQUIDITY:
        return None
    if volume < MIN_VOLUME_24H:
        return None
    if yes_price < MIN_PRICE or yes_price > MAX_PRICE:
        return None

    prev_price = safe_float(prev.get("yes_price"))
    prev_volume = safe_float(prev.get("volume"))

    d_price_abs = (yes_price - prev_price) if prev_price > 0 else 0.0
    d_price_pct = pct_change(prev_price, yes_price) if prev_price > 0 else 0.0
    d_volume = (volume - prev_volume) if prev_volume > 0 else 0.0

    # Triggers: price move + volume delta
    price_trigger = abs(d_price_abs) >= MIN_ABS_PRICE_MOVE and abs(d_price_pct) >= MIN_PCT_PRICE_MOVE
    vol_trigger = d_volume >= MIN_VOLUME_DELTA

    if not (price_trigger and vol_trigger):
        return None

    # Severity heuristic
    severity = "WATCH"
    if abs(d_price_pct) >= (MIN_PCT_PRICE_MOVE * 2) and d_volume >= (MIN_VOLUME_DELTA * 2):
        severity = "STRONG"

    direction = "up" if d_price_abs > 0 else "down"
    reason = (
        f"Price moved <b>{direction}</b> with a meaningful volume burst. "
        f"This often happens when new info hits the market or whales reposition."
    )

    return Signal(
        market_id=mid,
        title=title,
        url=market_url(m),
        yes_price=yes_price,
        liquidity=liquidity,
        volume=volume,
        d_price_abs=d_price_abs,
        d_price_pct=d_price_pct,
        d_volume=d_volume,
        reason=reason,
        severity=severity,
    )

def should_send(state: Dict[str, Any], mid: str) -> bool:
    mstate = state["markets"].get(mid, {})
    last_alert = int(mstate.get("last_alert_ts", 0))
    return (now_ts() - last_alert) >= (ALERT_COOLDOWN_MIN * 60)

def format_signal(sig: Signal) -> str:
    arrow = "📈" if sig.d_price_abs > 0 else "📉"
    sev = "🚨 <b>OPPORTUNITY (STRONG)</b>" if sig.severity == "STRONG" else "⚠️ <b>OPPORTUNITY (WATCH)</b>"

    msg = (
        f"{sev}\n"
        f"{arrow} <b>{clip(sig.title, 140)}</b>\n\n"
        f"YES price: <b>{sig.yes_price:.3f}</b>\n"
        f"Δ price: <b>{sig.d_price_abs:+.3f}</b> ({sig.d_price_pct:+.2f}%)\n"
        f"Δ volume (since last check): <b>{sig.d_volume:,.0f}</b>\n"
        f"Liquidity: <b>{sig.liquidity:,.0f}</b> | Volume: <b>{sig.volume:,.0f}</b>\n\n"
        f"Why it popped: {sig.reason}\n\n"
        f"Quick checklist before entering:\n"
        f"• What news/event could justify this move?\n"
        f"• Is liquidity real (orderbook depth) or thin?\n"
        f"• Is it close to resolution / any rule gotchas?\n"
        f"• If you’re wrong, where is your exit?\n\n"
        f"{sig.url}"
    )
    return msg


# -----------------------------
# MAIN LOOP
# -----------------------------
def run_once(state: Dict[str, Any]) -> Tuple[int, int]:
    scanned = 0
    sent = 0

    offset = 0
    remaining = MARKET_LIMIT_PER_POLL

    while remaining > 0:
        batch = min(100, remaining)
        markets = list_markets(limit=batch, offset=offset, status="open")
        if not markets:
            break

        for m in markets:
            scanned += 1
            mid = str(m.get("id") or m.get("conditionId") or "")
            if not mid:
                continue

            prev = state["markets"].get(mid, {})
            sig = compute_signal(m, prev)

            # Update snapshot regardless (so deltas work next round)
            yes_price = extract_best_yes_price(m)
            state["markets"][mid] = {
                "yes_price": yes_price if yes_price is not None else prev.get("yes_price", 0),
                "volume": safe_float(m.get("volume") or m.get("volumeNum") or m.get("volumeUSD") or m.get("volume24h")),
                "liquidity": safe_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidityUSD")),
                "title": (m.get("question") or m.get("title") or prev.get("title") or ""),
                "last_seen_ts": now_ts(),
                "last_alert_ts": prev.get("last_alert_ts", 0),
            }

            if sig and should_send(state, mid):
                tg_send(format_signal(sig))
                state["markets"][mid]["last_alert_ts"] = now_ts()
                sent += 1

        offset += len(markets)
        remaining -= len(markets)

        if len(markets) < batch:
            break

    state["last_run"] = now_ts()
    return scanned, sent

def main():
    state = load_state()

    # One-time startup ping (only if first run or state empty)
    if state.get("last_run", 0) == 0:
        tg_send("🤖 Bot online. Scanning Polymarket for volume+price dislocations (opportunity alerts).")

    while True:
        try:
            scanned, sent = run_once(state)
            save_state(state)

            # Silent by default (no annoying status spam)
            # But if you want a heartbeat, set HEARTBEAT=1
            if os.getenv("HEARTBEAT", "0") == "1":
                tg_send(f"✅ Heartbeat: scanned={scanned}, alerts={sent}")

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            print("Stopping...")
            break
        except Exception as e:
            # Send one concise error, then keep going
            err = f"❌ Bot error: {type(e).__name__}: {str(e)[:200]}\n\n{traceback.format_exc()[-900:]}"
            try:
                tg_send(err)
            except Exception:
                print(err)
            time.sleep(max(30, POLL_SECONDS))

if __name__ == "__main__":
    main()
