#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot (AGGRESSIVE_EDGE)
- More alerts (lower thresholds)
- 3-tier alerts: STRONG / TACTICAL / WEAK
- Re-alerts every ~10-15 min OR sooner if big change
- No annoying health/status spam

ENV REQUIRED:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

OPTIONAL ENV:
  POLY_ENDPOINT            # If you have your own JSON endpoint (list of markets)
  SCAN_EVERY_SEC=180       # default 180s (3 minutes)
  MAX_MARKETS=300          # default 300
  COOLDOWN_SEC=900         # default 900 (15 min)
  REARM_MOVE_PCT=1.2       # price move % to bypass cooldown
  GAP_MIN=0.008            # 0.8% spread threshold
  SCORE_MIN=6.5
  VOL_MIN=10000
  LIQ_MIN=5000
"""

import os
import time
import math
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

# ----------------------------
# Telegram
# ----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Try telebot first (PyTelegramBotAPI). If not available, fallback to raw HTTP.
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
        # Raw Telegram API fallback
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "disable_web_page_preview": True,
        }
        requests.post(url, json=payload, timeout=15).raise_for_status()


# ----------------------------
# Config (Aggressive defaults)
# ----------------------------
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()

SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "180"))          # 3 min
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "300"))               # how many to scan
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "900"))             # 15 min cooldown
REARM_MOVE_PCT = float(os.getenv("REARM_MOVE_PCT", "1.2"))       # bypass cooldown if move > 1.2%

GAP_MIN = float(os.getenv("GAP_MIN", "0.008"))                   # 0.8%
SCORE_MIN = float(os.getenv("SCORE_MIN", "6.5"))
VOL_MIN = float(os.getenv("VOL_MIN", "10000"))
LIQ_MIN = float(os.getenv("LIQ_MIN", "5000"))

HTTP_TIMEOUT = 20
UA = {"User-Agent": "AGGRESSIVE_EDGE_BOT/1.0"}


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


def _best_yes_price(m: Dict[str, Any]) -> Optional[float]:
    """
    Try hard to find a 'YES' price in many common Polymarket/Gamma schemas.
    Returns a float in [0,1] or None.
    """
    # Common Gamma: outcomePrices might be list ["0.43","0.57"] + outcomes ["Yes","No"]
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices") or m.get("outcome_prices")

    if outcomes and outcome_prices and isinstance(outcomes, list) and isinstance(outcome_prices, list):
        # Find index of "Yes"
        idx = None
        for i, o in enumerate(outcomes):
            if str(o).strip().lower() == "yes":
                idx = i
                break
        if idx is not None and idx < len(outcome_prices):
            p = _to_float(outcome_prices[idx], default=-1.0)
            if 0.0 <= p <= 1.0:
                return p

    # Sometimes "prices" dict
    for key in ["yesPrice", "yes_price", "p_yes", "probability_yes", "probYes", "price_yes"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p

    # Sometimes orderbook fields
    for key in ["bestAsk", "best_ask", "ask", "yesAsk", "yes_ask"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p

    # If only "lastTradePrice"
    for key in ["lastTradePrice", "last_trade_price", "last", "lastPrice"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p

    return None


def _spread(m: Dict[str, Any]) -> float:
    """
    Estimate spread as ask - bid if available; else 0.
    """
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
    if bid is None or ask is None:
        return 0.0
    return max(0.0, ask - bid)


def _volume(m: Dict[str, Any]) -> float:
    for k in ["volume", "volume24hr", "volume24h", "volume_24h", "volumeUsd", "volumeUSD", "volume_usd"]:
        if k in m:
            v = _to_float(m.get(k), default=0.0)
            if v > 0:
                return v
    # Sometimes nested
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


def _title(m: Dict[str, Any]) -> str:
    for k in ["title", "question", "name", "marketTitle"]:
        if k in m and m.get(k):
            return _clean(str(m.get(k)))
    # Sometimes event title + market title
    ev = m.get("event") or {}
    if isinstance(ev, dict) and ev.get("title"):
        return _clean(str(ev.get("title")))
    return "Untitled market"


def _url(m: Dict[str, Any]) -> str:
    # If already provided
    for k in ["url", "marketUrl", "market_url", "link"]:
        if k in m and m.get(k):
            return str(m.get(k)).strip()

    # Gamma sometimes provides "slug"
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"

    # Sometimes "id" that maps to /event/<id> (not always correct, but better than nothing)
    mid = m.get("id") or m.get("marketId") or m.get("market_id")
    if mid:
        return f"https://polymarket.com/event/{mid}"

    return "https://polymarket.com/markets"


def _market_key(m: Dict[str, Any]) -> str:
    # stable key for cooldown cache
    for k in ["id", "marketId", "market_id", "conditionId", "condition_id", "slug"]:
        if k in m and m.get(k):
            return str(m.get(k))
    return _title(m)[:80]


def _recommendation_from_move(price_now: float, price_prev: Optional[float]) -> Tuple[str, str]:
    """
    Simple momentum recommendation:
      if price rising -> lean YES
      if falling -> lean NO
      else -> WATCH
    """
    if price_prev is None:
        return ("WATCH", "no recent baseline yet")
    delta = price_now - price_prev
    if abs(delta) < 0.002:
        return ("WATCH", "flat since last scan")
    if delta > 0:
        return ("LEAN YES", f"momentum up (+{delta:.3f})")
    return ("LEAN NO", f"momentum down ({delta:.3f})")


def _score(vol: float, liq: float, spread: float, move_abs: float) -> float:
    """
    Aggressive heuristic score (higher = more actionable)
    - rewards spread (inefficiency) + movement + liquidity/volume
    """
    # normalize
    vol_term = math.log10(max(vol, 1.0))          # ~ 4 to 7 typical
    liq_term = math.log10(max(liq, 1.0))          # ~ 3 to 7 typical

    spread_term = (spread * 100.0) * 0.85         # spread in cents, weighted
    move_term = (move_abs * 100.0) * 0.60         # move in cents, weighted

    # base + small bonus for "healthy" markets
    base = 1.5
    s = base + vol_term + liq_term + spread_term + move_term
    return float(_clamp(s, 0.0, 20.0))


def _tier(score: float, spread: float, move_abs: float) -> str:
    # STRONG: big dislocation or very high score
    if score >= 10.0 or spread >= 0.02 or move_abs >= 0.03:
        return "STRONG"
    if score >= 7.5:
        return "TACTICAL"
    return "WEAK"


# ----------------------------
# Polymarket fetch
# ----------------------------
def fetch_markets() -> List[Dict[str, Any]]:
    """
    Fetch markets from:
    1) POLY_ENDPOINT (user-provided JSON)
    2) Polymarket Gamma API as fallback
    """
    if POLY_ENDPOINT:
        r = requests.get(POLY_ENDPOINT, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
            return data["markets"]
        if isinstance(data, list):
            return data
        return []

    # Fallback: Gamma API (public)
    # Note: schema can vary; we parse defensively.
    url = "https://gamma-api.polymarket.com/markets"
    out: List[Dict[str, Any]] = []
    limit = min(MAX_MARKETS, 200)  # gamma limit often 200
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
        if offset > 1000:  # safety
            break

    return out[:MAX_MARKETS]


# ----------------------------
# Main loop
# ----------------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        print("Set them in Railway -> Variables -> Production.")
        return

    # Cooldown + history
    last_sent_ts: Dict[str, float] = {}
    last_price: Dict[str, float] = {}
    last_vol: Dict[str, float] = {}

    send_telegram("🤖 Bot ON: AGGRESSIVE_EDGE (more alerts, 3 tiers, 3-min scans).")

    while True:
        try:
            markets = fetch_markets()
        except Exception as e:
            # No spam: only one error every 20 min
            now = time.time()
            key = "FETCH_ERROR"
            if now - last_sent_ts.get(key, 0) > 1200:
                send_telegram(f"⚠️ Fetch error: {type(e).__name__}: {e}")
                last_sent_ts[key] = now
            time.sleep(SCAN_EVERY_SEC)
            continue

        now = time.time()
        alerts_sent = 0

        for m in markets:
            try:
                title = _title(m)
                key = _market_key(m)
                url = _url(m)

                p = _best_yes_price(m)
                if p is None:
                    continue
                p = _clamp(p, 0.0, 1.0)

                spread = _spread(m)  # 0..1
                vol = _volume(m)
                liq = _liquidity(m)

                # Aggressive filters (still minimal sanity)
                if vol < VOL_MIN and liq < LIQ_MIN:
                    continue
                if spread < GAP_MIN:
                    # still allow if big move (tactical)
                    pass

                prev_p = last_price.get(key)
                move_abs = abs(p - prev_p) if prev_p is not None else 0.0

                # Volume delta (if available)
                prev_vol = last_vol.get(key, vol)
                vol_delta = max(0.0, vol - prev_vol)

                score = _score(vol=vol, liq=liq, spread=spread, move_abs=move_abs)

                # Gate by score, but allow if spread good or move big
                if score < SCORE_MIN and spread < GAP_MIN and move_abs < 0.015:
                    last_price[key] = p
                    last_vol[key] = vol
                    continue

                # Cooldown logic (aggressive rearm)
                t_last = last_sent_ts.get(key, 0.0)
                cooldown_ok = (now - t_last) >= COOLDOWN_SEC
                rearm_ok = prev_p is not None and (abs(p - prev_p) / max(prev_p, 1e-9) * 100.0) >= REARM_MOVE_PCT

                if not cooldown_ok and not rearm_ok:
                    last_price[key] = p
                    last_vol[key] = vol
                    continue

                tier = _tier(score=score, spread=spread, move_abs=move_abs)
                rec, rec_reason = _recommendation_from_move(price_now=p, price_prev=prev_p)

                # Make message very explicit
                # Show "YES price" and implied "NO price" (approx)
                no_price = _clamp(1.0 - p, 0.0, 1.0)

                msg = (
                    f"🚨 {tier} | Edge scan\n"
                    f"🎯 ACTION: {rec}\n"
                    f"🧠 Why: {rec_reason} | Spread={_pct(spread)} | Move={_pct(move_abs)}\n"
                    f"💰 YES={p:.3f} | NO≈{no_price:.3f}\n"
                    f"📊 Vol={vol:,.0f} (Δ{vol_delta:,.0f}) | Liq={liq:,.0f} | Score={score:.2f}\n"
                    f"📝 {title}\n"
                    f"{url}"
                )

                send_telegram(msg)
                last_sent_ts[key] = now
                alerts_sent += 1

                # Hard cap per scan to avoid floods
                if alerts_sent >= 12:
                    break

                last_price[key] = p
                last_vol[key] = vol

            except Exception:
                # swallow per-market parse errors silently (no spam)
                continue

        # Update cached prices for markets we didn’t alert on as well
        # (we already update inside loop when processing)

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
