#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot (AGGRESSIVE_EDGE v2)
- More alerts (lower thresholds)
- Recommendation ALWAYS explicit: ENTER YES / ENTER NO / WAIT
- Explains spread clearly (bid/ask gap)
- Re-alerts every ~10-15 min OR sooner if big change
- No health/status spam

ENV REQUIRED:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

OPTIONAL ENV:
  POLY_ENDPOINT
  SCAN_EVERY_SEC=180
  MAX_MARKETS=300
  COOLDOWN_SEC=900
  REARM_MOVE_PCT=1.2
  GAP_MIN=0.008
  SCORE_MIN=6.2
  VOL_MIN=10000
  LIQ_MIN=5000
"""

import os
import time
import math
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

# ----------------------------
# Telegram
# ----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

try:
    import telebot  # type: ignore
    _HAS_TELEBOT = True
except Exception:
    _HAS_TELEBOT = False


def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return

    msg = msg.strip()
    if not msg:
        return

    if _HAS_TELEBOT:
        bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)
        bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
        requests.post(url, json=payload, timeout=15).raise_for_status()


# ----------------------------
# Config
# ----------------------------
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()

SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "180"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "300"))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "900"))
REARM_MOVE_PCT = float(os.getenv("REARM_MOVE_PCT", "1.2"))

GAP_MIN = float(os.getenv("GAP_MIN", "0.008"))       # 0.8%
SCORE_MIN = float(os.getenv("SCORE_MIN", "6.2"))
VOL_MIN = float(os.getenv("VOL_MIN", "10000"))
LIQ_MIN = float(os.getenv("LIQ_MIN", "5000"))

HTTP_TIMEOUT = 20
UA = {"User-Agent": "AGGRESSIVE_EDGE_BOT/2.0"}


# ----------------------------
# Helpers
# ----------------------------
def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "":
            return default
        return float(s)
    except Exception:
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _title(m: Dict[str, Any]) -> str:
    for k in ["title", "question", "name", "marketTitle"]:
        if k in m and m.get(k):
            return _clean(str(m.get(k)))
    ev = m.get("event") or {}
    if isinstance(ev, dict) and ev.get("title"):
        return _clean(str(ev.get("title")))
    return "Untitled market"


def _url(m: Dict[str, Any]) -> str:
    for k in ["url", "marketUrl", "market_url", "link"]:
        if k in m and m.get(k):
            return str(m.get(k)).strip()
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = m.get("id") or m.get("marketId") or m.get("market_id")
    if mid:
        return f"https://polymarket.com/event/{mid}"
    return "https://polymarket.com/markets"


def _market_key(m: Dict[str, Any]) -> str:
    for k in ["id", "marketId", "market_id", "conditionId", "condition_id", "slug"]:
        if k in m and m.get(k):
            return str(m.get(k))
    return _title(m)[:80]


def _best_yes_price(m: Dict[str, Any]) -> Optional[float]:
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices") or m.get("outcome_prices")
    if outcomes and outcome_prices and isinstance(outcomes, list) and isinstance(outcome_prices, list):
        idx = None
        for i, o in enumerate(outcomes):
            if str(o).strip().lower() == "yes":
                idx = i
                break
        if idx is not None and idx < len(outcome_prices):
            p = _to_float(outcome_prices[idx], default=-1.0)
            if 0.0 <= p <= 1.0:
                return p
    for key in ["yesPrice", "yes_price", "p_yes", "probYes", "lastTradePrice", "last_trade_price", "last", "lastPrice"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p
    return None


def _best_bid_ask(m: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    bid_keys = ["bestBid", "best_bid", "bid", "yesBid", "yes_bid"]
    ask_keys = ["bestAsk", "best_ask", "ask", "yesAsk", "yes_ask"]
    bid = None
    ask = None
    for k in bid_keys:
        if k in m:
            v = _to_float(m.get(k), default=-1.0)
            if 0.0 <= v <= 1.0:
                bid = v
                break
    for k in ask_keys:
        if k in m:
            v = _to_float(m.get(k), default=-1.0)
            if 0.0 <= v <= 1.0:
                ask = v
                break
    return bid, ask


def _spread(m: Dict[str, Any]) -> float:
    bid, ask = _best_bid_ask(m)
    if bid is None or ask is None:
        return 0.0
    return max(0.0, ask - bid)


def _volume(m: Dict[str, Any]) -> float:
    for k in ["volume", "volume24hr", "volume24h", "volume_24h", "volumeUsd", "volumeUSD", "volume_usd"]:
        if k in m:
            v = _to_float(m.get(k), default=0.0)
            if v > 0:
                return v
    v = m.get("volume")
    if isinstance(v, dict):
        for kk in ["usd", "USD", "24h", "24hr"]:
            if kk in v:
                vv = _to_float(v.get(kk), default=0.0)
                if vv > 0:
                    return vv
    return 0.0


def _liquidity(m: Dict[str, Any]) -> float:
    for k in ["liquidity", "liquidityUSD", "liquidityUsd", "liquidity_usd", "openInterest", "open_interest"]:
        if k in m:
            v = _to_float(m.get(k), default=0.0)
            if v > 0:
                return v
    return 0.0


def _score(vol: float, liq: float, spread: float, move_abs: float) -> float:
    vol_term = math.log10(max(vol, 1.0))
    liq_term = math.log10(max(liq, 1.0))
    spread_term = (spread * 100.0) * 0.9
    move_term = (move_abs * 100.0) * 0.7
    base = 1.2
    s = base + vol_term + liq_term + spread_term + move_term
    return float(_clamp(s, 0.0, 20.0))


def _tier(score: float, spread: float, move_abs: float) -> str:
    if score >= 10.0 or spread >= 0.02 or move_abs >= 0.03:
        return "STRONG"
    if score >= 7.4:
        return "TACTICAL"
    return "WEAK"


def _recommendation_explicit(
    title: str,
    yes_price: float,
    prev_yes: Optional[float],
    spread: float,
    liq: float,
) -> Tuple[str, str]:
    """
    Returns (action, why)
    action: "ENTER YES" | "ENTER NO" | "WAIT"
    """

    # If we have momentum info
    if prev_yes is not None:
        d = yes_price - prev_yes
        if abs(d) >= 0.006:
            if d > 0:
                return ("ENTER YES (A FAVOR)", f"momentum up (+{d:.3f})")
            else:
                return ("ENTER NO (CONTRA)", f"momentum down ({d:.3f})")

    # No strong momentum (or no baseline): use price-location logic
    # YES very cheap (<0.35): favor buying YES; YES very expensive (>0.65): favor buying NO
    if yes_price <= 0.35:
        return ("ENTER YES (A FAVOR)", "YES is cheap (asymmetric upside if it rebounds)")
    if yes_price >= 0.65:
        return ("ENTER NO (CONTRA)", "YES is expensive (better value on NO side)")

    # Mid-zone: only enter if market is tradable
    if spread <= 0.012 and liq >= 10000:
        # In mid-zone, lean with micro-bias by title keywords (optional, simple)
        # But we keep it neutral to avoid false confidence
        return ("WAIT / WATCH", "mid-price zone (0.35–0.65): need clearer move or catalyst")
    return ("WAIT / WATCH", "spread/liquidity not ideal for clean entry")


# ----------------------------
# Fetch
# ----------------------------
def fetch_markets() -> List[Dict[str, Any]]:
    if POLY_ENDPOINT:
        r = requests.get(POLY_ENDPOINT, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
            return data["markets"]
        if isinstance(data, list):
            return data
        return []

    url = "https://gamma-api.polymarket.com/markets"
    out: List[Dict[str, Any]] = []
    limit = min(MAX_MARKETS, 200)
    offset = 0

    while len(out) < MAX_MARKETS:
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": str(offset),
            "order": "volume",
            "ascending": "false",
        }
        r = requests.get(url, params=params, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
        if offset > 1000:
            break

    return out[:MAX_MARKETS]


# ----------------------------
# Main loop
# ----------------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return

    last_sent_ts: Dict[str, float] = {}
    last_yes: Dict[str, float] = {}
    last_vol: Dict[str, float] = {}

    send_telegram("🤖 Bot ON: AGGRESSIVE_EDGE v2 (explicit ENTER YES/NO, more alerts).")

    while True:
        try:
            markets = fetch_markets()
        except Exception as e:
            now = time.time()
            if now - last_sent_ts.get("FETCH_ERROR", 0) > 1200:
                send_telegram(f"⚠️ Fetch error: {type(e).__name__}: {e}")
                last_sent_ts["FETCH_ERROR"] = now
            time.sleep(SCAN_EVERY_SEC)
            continue

        now = time.time()
        alerts_sent = 0

        for m in markets:
            try:
                key = _market_key(m)
                title = _title(m)
                url = _url(m)

                yes = _best_yes_price(m)
                if yes is None:
                    continue
                yes = _clamp(yes, 0.0, 1.0)

                bid, ask = _best_bid_ask(m)
                spread = _spread(m)
                vol = _volume(m)
                liq = _liquidity(m)

                # Minimal sanity filters (aggressive)
                if vol < VOL_MIN and liq < LIQ_MIN:
                    continue

                prev_yes = last_yes.get(key)
                move_abs = abs(yes - prev_yes) if prev_yes is not None else 0.0

                prev_vol = last_vol.get(key, vol)
                vol_delta = max(0.0, vol - prev_vol)

                score = _score(vol=vol, liq=liq, spread=spread, move_abs=move_abs)

                # Gate (still aggressive)
                if score < SCORE_MIN and spread < GAP_MIN and move_abs < 0.012:
                    last_yes[key] = yes
                    last_vol[key] = vol
                    continue

                # Cooldown + rearm
                t_last = last_sent_ts.get(key, 0.0)
                cooldown_ok = (now - t_last) >= COOLDOWN_SEC
                rearm_ok = prev_yes is not None and (abs(yes - prev_yes) / max(prev_yes, 1e-9) * 100.0) >= REARM_MOVE_PCT

                if not cooldown_ok and not rearm_ok:
                    last_yes[key] = yes
                    last_vol[key] = vol
                    continue

                tier = _tier(score=score, spread=spread, move_abs=move_abs)
                action, why = _recommendation_explicit(title, yes, prev_yes, spread, liq)

                no = _clamp(1.0 - yes, 0.0, 1.0)

                # Spread explanation in message
                spread_cents = spread * 100.0
                bid_s = f"{bid:.3f}" if bid is not None else "n/a"
                ask_s = f"{ask:.3f}" if ask is not None else "n/a"

                msg = (
                    f"🚨 {tier} | ALERT\n"
                    f"✅ RECOMMENDATION: {action}\n"
                    f"🧠 Reason: {why}\n"
                    f"💰 YES={yes:.3f} | NO≈{no:.3f} | Move={_pct(move_abs)}\n"
                    f"📌 Spread: {spread_cents:.2f}¢ ({_pct(spread)})  [bid={bid_s} / ask={ask_s}]\n"
                    f"📊 Vol={vol:,.0f} (Δ{vol_delta:,.0f}) | Liq={liq:,.0f} | Score={score:.2f}\n"
                    f"📝 {title}\n"
                    f"{url}"
                )

                send_telegram(msg)
                last_sent_ts[key] = now
                alerts_sent += 1

                if alerts_sent >= 14:
                    break

                last_yes[key] = yes
                last_vol[key] = vol

            except Exception:
                continue

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
