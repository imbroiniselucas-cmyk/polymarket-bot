#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot (AGGRESSIVE_EDGE v4)
FOCUS:
  1) Arbitrage (risk-free) when possible: YES_ASK + NO_ASK < 1 - buffer
  2) High moves: price move % + flow move % (volume delta %)
  3) News context: pulls latest headlines via GDELT (no API key)

REQUIRED ENV:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

OPTIONAL ENV:
  POLY_ENDPOINT
  SCAN_EVERY_SEC=180
  MAX_MARKETS=400
  COOLDOWN_SEC=600

  # High-move triggers
  PRICE_MOVE_ALERT_PCT=4.0      # alert if YES price changes >= 4% since last scan
  FLOW_MOVE_ALERT_PCT=12.0      # alert if volume changes >= 12% since last scan
  ABS_PRICE_MOVE_ALERT=0.020    # alert if absolute YES change >= 2.0 cents

  # Arbitrage triggers (risk buffer)
  ARB_BUFFER=0.004              # require at least 0.4% edge after slippage

  # General filters
  VOL_MIN=8000
  LIQ_MIN=4000

  # Weather/Climate focus cities (title match only)
  (hardcoded: London, Buenos Aires, Ankara)
"""

import os
import time
import math
import re
import datetime as dt
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
    msg = (msg or "").strip()
    if not msg:
        return

    if _HAS_TELEBOT:
        bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)
        bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
        requests.post(url, json=payload, timeout=15).raise_for_status()


# ----------------------------
# Config
# ----------------------------
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()

SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "180"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "400"))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "600"))

PRICE_MOVE_ALERT_PCT = float(os.getenv("PRICE_MOVE_ALERT_PCT", "4.0")) / 100.0
FLOW_MOVE_ALERT_PCT = float(os.getenv("FLOW_MOVE_ALERT_PCT", "12.0")) / 100.0
ABS_PRICE_MOVE_ALERT = float(os.getenv("ABS_PRICE_MOVE_ALERT", "0.020"))

ARB_BUFFER = float(os.getenv("ARB_BUFFER", "0.004"))

VOL_MIN = float(os.getenv("VOL_MIN", "8000"))
LIQ_MIN = float(os.getenv("LIQ_MIN", "4000"))

HTTP_TIMEOUT = 20
UA = {"User-Agent": "AGGRESSIVE_EDGE_BOT/4.0"}

# Cities you asked for
CITY_KEYS = ["london", "buenos aires", "ankara"]


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
    return _title(m)[:90]


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


# ----------------------------
# Price / orderbook parsing (defensive)
# ----------------------------
def _yes_no_from_outcome_prices(m: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    outcomePrices + outcomes sometimes contain mid prices for YES/NO (not bids/asks).
    Still useful for move alerts & fallback.
    """
    outcomes = m.get("outcomes")
    prices = m.get("outcomePrices") or m.get("outcome_prices")
    if not (isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) == len(prices)):
        return (None, None)

    yes = no = None
    for i, o in enumerate(outcomes):
        name = str(o).strip().lower()
        p = _to_float(prices[i], default=-1.0)
        if not (0.0 <= p <= 1.0):
            continue
        if name == "yes":
            yes = p
        elif name == "no":
            no = p
    return (yes, no)


def _extract_side_prices(m: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """
    Try many common key patterns for YES/NO bid/ask.
    Not all markets expose this; if missing, arb may not be computable.
    """
    keys = {
        "yes_bid": ["yesBid", "yes_bid", "bestBidYes", "best_bid_yes", "bid_yes"],
        "yes_ask": ["yesAsk", "yes_ask", "bestAskYes", "best_ask_yes", "ask_yes"],
        "no_bid":  ["noBid", "no_bid", "bestBidNo", "best_bid_no", "bid_no"],
        "no_ask":  ["noAsk", "no_ask", "bestAskNo", "best_ask_no", "ask_no"],
    }
    out: Dict[str, Optional[float]] = {k: None for k in keys}

    for out_k, in_keys in keys.items():
        for kk in in_keys:
            if kk in m:
                v = _to_float(m.get(kk), default=-1.0)
                if 0.0 <= v <= 1.0:
                    out[out_k] = v
                    break

    # fallback: some schemas only have bestBid/bestAsk without side; treat as YES
    if out["yes_bid"] is None and "bestBid" in m:
        v = _to_float(m.get("bestBid"), default=-1.0)
        if 0.0 <= v <= 1.0:
            out["yes_bid"] = v
    if out["yes_ask"] is None and "bestAsk" in m:
        v = _to_float(m.get("bestAsk"), default=-1.0)
        if 0.0 <= v <= 1.0:
            out["yes_ask"] = v

    return out


def _best_yes_price(m: Dict[str, Any]) -> Optional[float]:
    """
    Prefer explicit YES mid/last; fallback to outcomePrices.
    """
    for key in ["yesPrice", "yes_price", "p_yes", "probYes", "lastTradePrice", "last_trade_price", "last", "lastPrice"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p
    yes, _no = _yes_no_from_outcome_prices(m)
    if yes is not None:
        return yes
    return None


# ----------------------------
# Arbitrage logic
# ----------------------------
def find_arbitrage(side: Dict[str, Optional[float]]) -> Optional[Dict[str, Any]]:
    """
    Risk-free arb if you can BUY both sides cheap enough:
      YES_ASK + NO_ASK < 1 - ARB_BUFFER
    """
    ya = side.get("yes_ask")
    na = side.get("no_ask")
    if ya is None or na is None:
        return None

    total = ya + na
    if total < (1.0 - ARB_BUFFER):
        profit = (1.0 - total)
        return {
            "type": "BUY_BOTH",
            "yes_ask": ya,
            "no_ask": na,
            "sum": total,
            "locked_profit": profit,
        }
    return None


# ----------------------------
# News via GDELT (no key)
# ----------------------------
STOPWORDS = {
    "will", "the", "a", "an", "to", "of", "in", "on", "by", "for", "and", "or", "is", "are", "be",
    "above", "below", "reach", "hit", "over", "under", "before", "after", "with", "at", "from",
    "london", "buenos", "aires", "ankara", "today", "tomorrow",
    "price", "bitcoin", "eth", "btc", "yes", "no"
}

def build_query_from_title(title: str) -> str:
    # extract meaningful tokens; keep uppercase tickers or long words
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", title)
    keep: List[str] = []
    for t in tokens:
        tl = t.lower()
        if tl in STOPWORDS:
            continue
        if len(t) >= 4:
            keep.append(t)
    # Use top 3–4 tokens
    keep = keep[:4]
    if not keep:
        # fallback: city-focused
        for c in CITY_KEYS:
            if c in title.lower():
                return c
        return title.split("?")[0][:40]
    return " AND ".join(keep)


def fetch_gdelt_headlines(query: str, max_items: int = 3) -> List[str]:
    """
    GDELT 2 DOC API - last ~24h of results.
    """
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_items),
        "timespan": "1d",
        "sourcelang": "English",
        "sort": "HybridRel",
    }
    try:
        r = requests.get(base, params=params, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        arts = data.get("articles", []) if isinstance(data, dict) else []
        out = []
        for a in arts[:max_items]:
            title = _clean(str(a.get("title", "")))
            source = _clean(str(a.get("sourceCountry", ""))) or _clean(str(a.get("source", "")))
            if title:
                out.append(f"• {title}" + (f" ({source})" if source else ""))
        return out
    except Exception:
        return []


# ----------------------------
# Fetch Polymarket markets
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
        if offset > 1200:
            break

    return out[:MAX_MARKETS]


# ----------------------------
# Alert formatting / decisions
# ----------------------------
def move_direction_label(yes_now: float, yes_prev: Optional[float]) -> str:
    if yes_prev is None:
        return "N/A"
    if yes_now > yes_prev:
        return "UP (toward YES)"
    if yes_now < yes_prev:
        return "DOWN (toward NO)"
    return "FLAT"


def explicit_trade_side_from_move(yes_now: float, yes_prev: Optional[float]) -> str:
    # Clear: if price jumped up, momentum is YES; if down, momentum is NO
    if yes_prev is None:
        return "WAIT"
    if yes_now > yes_prev:
        return "ENTER YES (A FAVOR)"
    if yes_now < yes_prev:
        return "ENTER NO (CONTRA)"
    return "WAIT"


def is_focus_city_market(title: str) -> bool:
    t = title.lower()
    return any(c in t for c in CITY_KEYS)


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

    send_telegram("🤖 Bot ON: v4 (ARB + HIGH MOVES + NEWS context).")

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
        sent = 0

        for m in markets:
            try:
                key = _market_key(m)
                title = _title(m)
                url = _url(m)

                vol = _volume(m)
                liq = _liquidity(m)

                if vol < VOL_MIN and liq < LIQ_MIN:
                    continue

                yes = _best_yes_price(m)
                if yes is None:
                    continue
                yes = _clamp(yes, 0.0, 1.0)

                prev_yes = last_yes.get(key)
                prev_vol = last_vol.get(key, vol)

                # Price moves
                abs_move = abs(yes - prev_yes) if prev_yes is not None else 0.0
                price_move_pct = (abs(yes - prev_yes) / max(prev_yes, 1e-9)) if prev_yes is not None else 0.0

                # Flow moves (volume)
                vol_delta = vol - prev_vol
                flow_move_pct = (abs(vol_delta) / max(prev_vol, 1e-9)) if prev_vol is not None else 0.0
                flow_dir = "IN" if vol_delta >= 0 else "OUT"

                # Arbitrage (if we can see both sides asks)
                side = _extract_side_prices(m)
                arb = find_arbitrage(side)

                # High move trigger
                high_move = (
                    (prev_yes is not None and (price_move_pct >= PRICE_MOVE_ALERT_PCT or abs_move >= ABS_PRICE_MOVE_ALERT))
                    or (prev_vol is not None and flow_move_pct >= FLOW_MOVE_ALERT_PCT)
                )

                # Focus: prioritize city markets (London/Buenos Aires/Ankara) but still scan all
                focus = is_focus_city_market(title)

                # Decide if we should alert
                should_alert = False
                alert_type = ""
                if arb is not None:
                    should_alert = True
                    alert_type = "ARB"
                elif high_move and (focus or vol >= (VOL_MIN * 1.5) or liq >= (LIQ_MIN * 1.5)):
                    should_alert = True
                    alert_type = "MOVE"

                if not should_alert:
                    last_yes[key] = yes
                    last_vol[key] = vol
                    continue

                # Cooldown (but let ARB through faster)
                cooldown = 240 if alert_type == "ARB" else COOLDOWN_SEC
                t_last = last_sent_ts.get(key, 0.0)
                if (now - t_last) < cooldown and alert_type != "ARB":
                    last_yes[key] = yes
                    last_vol[key] = vol
                    continue

                # News context
                q = build_query_from_title(title)
                headlines = fetch_gdelt_headlines(q, max_items=3)
                news_block = "\n".join(headlines) if headlines else "• (no recent headlines found via GDELT)"

                # Compose recommendation (clear)
                if alert_type == "ARB":
                    rec = "ENTER BOTH SIDES (ARBITRAGE)"
                    ya = arb["yes_ask"]
                    na = arb["no_ask"]
                    locked = arb["locked_profit"]
                    why = f"YES_ASK + NO_ASK = {arb['sum']:.3f} < 1.000 (buffer {ARB_BUFFER:.3f}) → locked profit ≈ {locked*100:.2f}%"
                    action_line = f"🎯 ACTION: Buy YES @ {ya:.3f} AND Buy NO @ {na:.3f}"
                else:
                    rec = explicit_trade_side_from_move(yes, prev_yes)
                    why = f"High move detected: PriceMove={_pct(price_move_pct)} (abs {abs_move:.3f}) | FlowMove%={_pct(flow_move_pct)} ({flow_dir} {abs(vol_delta):,.0f})"
                    action_line = f"🎯 ACTION: {rec}"

                # Extra: show whether move favors YES/NO
                move_dir = move_direction_label(yes, prev_yes)

                msg = (
                    f"🚨 {alert_type} ALERT\n"
                    f"{action_line}\n"
                    f"🧠 Reason: {why}\n"
                    f"💰 YES={yes:.3f} | NO≈{(1-yes):.3f} | MoveDir={move_dir}\n"
                    f"💸 Flow: {flow_dir} {abs(vol_delta):,.0f} | FlowMove%={_pct(flow_move_pct)}  (Vol {prev_vol:,.0f}→{vol:,.0f})\n"
                    f"📊 Liq={liq:,.0f}\n"
                    f"🗞 News (last 24h):\n{news_block}\n"
                    f"📝 {title}\n"
                    f"{url}"
                )

                send_telegram(msg)
                last_sent_ts[key] = now
                sent += 1

                # Anti-flood cap
                if sent >= 18:
                    break

                last_yes[key] = yes
                last_vol[key] = vol

            except Exception:
                continue

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
