#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot (EDGE v5) — OPPORTUNITY-FIRST
Fixes your two pain points:
1) NEWS relevance: stops “random headlines” by scoring headlines vs extracted keywords + city focus.
2) LATE moves: avoids alerting after the market is already dead (price ~0/1) or the move already faded.

What it does:
A) ARBITRAGE (risk-free) if YES_ASK + NO_ASK < 1 - buffer
B) FRESH MOVE opportunities (not stale):
   - triggers only if move is happening NOW (last scan AND continuing),
   - and current price is not already ~0 or ~1 (unless arb).
C) NEWS used only when it helps:
   - pulls last 6h via GDELT, then RELEVANCE-SCORES headlines.
   - if relevance score is low, it labels it “no catalyst found” and does NOT pretend.

ENV REQUIRED:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

OPTIONAL ENV:
  POLY_ENDPOINT
  SCAN_EVERY_SEC=180
  MAX_MARKETS=400

  # Filters
  VOL_MIN=12000
  LIQ_MIN=6000

  # Fresh-move detection
  PRICE_MOVE_ALERT_PCT=6.0         # >= 6% in last scan
  ABS_PRICE_MOVE_ALERT=0.020       # or >= 2.0 cents
  FLOW_MOVE_ALERT_PCT=15.0         # volume delta >= 15% in last scan

  # Avoid late alerts
  SKIP_IF_PRICE_BELOW=0.01
  SKIP_IF_PRICE_ABOVE=0.99
  STALE_RETRACE_CENTS=0.010        # if it moved but already retraced a lot, skip

  # Arbitrage
  ARB_BUFFER=0.004

  # News
  NEWS_TIMESPAN=6h                 # (fixed in code)
  NEWS_MAX=5
  NEWS_MIN_SCORE=2                 # minimum keyword hits across headlines to count as relevant

Cities focus for weather/climate: London, Buenos Aires, Ankara
"""

import os
import time
import re
from typing import Any, Dict, List, Optional, Tuple
from collections import deque, Counter

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

VOL_MIN = float(os.getenv("VOL_MIN", "12000"))
LIQ_MIN = float(os.getenv("LIQ_MIN", "6000"))

PRICE_MOVE_ALERT_PCT = float(os.getenv("PRICE_MOVE_ALERT_PCT", "6.0")) / 100.0
ABS_PRICE_MOVE_ALERT = float(os.getenv("ABS_PRICE_MOVE_ALERT", "0.020"))
FLOW_MOVE_ALERT_PCT = float(os.getenv("FLOW_MOVE_ALERT_PCT", "15.0")) / 100.0

SKIP_IF_PRICE_BELOW = float(os.getenv("SKIP_IF_PRICE_BELOW", "0.01"))
SKIP_IF_PRICE_ABOVE = float(os.getenv("SKIP_IF_PRICE_ABOVE", "0.99"))
STALE_RETRACE_CENTS = float(os.getenv("STALE_RETRACE_CENTS", "0.010"))

ARB_BUFFER = float(os.getenv("ARB_BUFFER", "0.004"))

NEWS_MAX = int(os.getenv("NEWS_MAX", "5"))
NEWS_MIN_SCORE = int(os.getenv("NEWS_MIN_SCORE", "2"))

HTTP_TIMEOUT = 20
UA = {"User-Agent": "EDGE_BOT/5.0"}

CITY_KEYS = ["london", "buenos aires", "ankara"]


# ----------------------------
# Small helpers
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


def _best_yes_price(m: Dict[str, Any]) -> Optional[float]:
    for key in ["yesPrice", "yes_price", "p_yes", "probYes", "lastTradePrice", "last_trade_price", "last", "lastPrice"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p
    yes, _ = _yes_no_from_outcome_prices(m)
    return yes


def _extract_side_prices(m: Dict[str, Any]) -> Dict[str, Optional[float]]:
    keys = {
        "yes_ask": ["yesAsk", "yes_ask", "bestAskYes", "best_ask_yes", "ask_yes"],
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
    return out


# ----------------------------
# Arbitrage
# ----------------------------
def find_arbitrage(side: Dict[str, Optional[float]]) -> Optional[Dict[str, Any]]:
    ya = side.get("yes_ask")
    na = side.get("no_ask")
    if ya is None or na is None:
        return None
    total = ya + na
    if total < (1.0 - ARB_BUFFER):
        return {"yes_ask": ya, "no_ask": na, "sum": total, "locked_profit": (1.0 - total)}
    return None


# ----------------------------
# News: relevance-scored (GDELT, no key)
# ----------------------------
STOP = {
    "will","the","a","an","to","of","in","on","by","for","and","or","is","are","be",
    "above","below","reach","hit","over","under","before","after","with","at","from",
    "today","tomorrow","price","yes","no","market","polymarket"
}
for c in CITY_KEYS:
    STOP |= set(c.split())

def extract_keywords(title: str) -> List[str]:
    # take meaningful tokens; prefer proper nouns / longer terms / tickers-like
    toks = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", title)
    out = []
    for t in toks:
        tl = t.lower()
        if tl in STOP:
            continue
        if len(t) >= 4:
            out.append(t)
    # keep most frequent unique in order
    seen = set()
    uniq = []
    for t in out:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            uniq.append(t)
    return uniq[:6]

def gdelt_fetch(query: str, max_items: int) -> List[Dict[str, str]]:
    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": str(max_items),
        "timespan": "6h",          # key change: last 6 hours (more timely)
        "sourcelang": "English",
        "sort": "HybridRel",
    }
    r = requests.get(base, params=params, headers=UA, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    arts = data.get("articles", []) if isinstance(data, dict) else []
    out = []
    for a in arts[:max_items]:
        title = _clean(str(a.get("title","")))
        src = _clean(str(a.get("source","")))
        if title:
            out.append({"title": title, "src": src})
    return out

def score_headlines(headlines: List[Dict[str,str]], keywords: List[str]) -> Tuple[int, List[str]]:
    # Score = total keyword hits across headlines (case-insensitive)
    kw = [k.lower() for k in keywords]
    scored_lines = []
    score = 0
    for h in headlines:
        ht = h["title"].lower()
        hits = sum(1 for k in kw if k in ht)
        if hits > 0:
            score += hits
            scored_lines.append(f"• {h['title']}" + (f" ({h['src']})" if h.get("src") else ""))
    return score, scored_lines

def get_relevant_news(title: str) -> Tuple[str, int]:
    kws = extract_keywords(title)

    # city markets: force city into query to avoid random results
    tlo = title.lower()
    city_q = None
    for c in CITY_KEYS:
        if c in tlo:
            city_q = c
            break

    # Build query: (city AND top keywords) OR just top keywords
    if city_q and kws:
        query = f"{city_q} AND ({' OR '.join(kws[:3])})"
    elif kws:
        query = " AND ".join(kws[:3])
    elif city_q:
        query = city_q
    else:
        query = title.split("?")[0][:50]

    try:
        heads = gdelt_fetch(query, NEWS_MAX)
    except Exception:
        return "• (news fetch failed)", 0

    score, good = score_headlines(heads, kws)

    # If nothing relevant, show honest line
    if score < NEWS_MIN_SCORE or not good:
        return "• (no relevant catalyst found in last 6h)", score

    return "\n".join(good[:3]), score


# ----------------------------
# Fetch markets
# ----------------------------
def fetch_markets() -> List[Dict[str, Any]]:
    if POLY_ENDPOINT:
        r = requests.get(POLY_ENDPOINT, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("markets"), list):
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
# Opportunity detection (fresh move, not stale)
# ----------------------------
def is_extreme_price(p: float) -> bool:
    return p <= SKIP_IF_PRICE_BELOW or p >= SKIP_IF_PRICE_ABOVE

def move_direction(now: float, prev: float) -> str:
    return "UP->YES" if now > prev else ("DOWN->NO" if now < prev else "FLAT")

def recommendation_from_direction(dir_label: str) -> str:
    if dir_label == "UP->YES":
        return "ENTER YES (A FAVOR)"
    if dir_label == "DOWN->NO":
        return "ENTER NO (CONTRA)"
    return "WAIT"

def is_continuing(prices: deque) -> bool:
    # continuing move: last 2 deltas same sign
    if len(prices) < 3:
        return False
    d1 = prices[-1] - prices[-2]
    d2 = prices[-2] - prices[-3]
    return (d1 > 0 and d2 > 0) or (d1 < 0 and d2 < 0)

def retraced_too_much(prices: deque) -> bool:
    # If it spiked but already came back, skip as “late”
    if len(prices) < 4:
        return False
    # Compare last to local extreme in last 4
    window = list(prices)[-4:]
    last = window[-1]
    mx = max(window); mn = min(window)
    # if moved up then fell back a lot OR moved down then bounced a lot
    if mx - mn < 0.015:
        return False
    # retrace amount from extreme to last
    retr = max(abs(mx - last), abs(last - mn))
    return retr >= STALE_RETRACE_CENTS


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return

    # history for freshness detection
    price_hist: Dict[str, deque] = {}
    vol_prev: Dict[str, float] = {}
    last_alert_ts: Dict[str, float] = {}

    send_telegram("🤖 Bot ON: EDGE v5 (ARB + FRESH MOVES + RELEVANT NEWS).")

    while True:
        try:
            markets = fetch_markets()
        except Exception as e:
            send_telegram(f"⚠️ Fetch error: {type(e).__name__}: {e}")
            time.sleep(SCAN_EVERY_SEC)
            continue

        now_ts = time.time()
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

                # Track history
                if key not in price_hist:
                    price_hist[key] = deque(maxlen=6)
                price_hist[key].append(yes)

                prev_v = vol_prev.get(key, vol)
                vol_delta = vol - prev_v
                flow_move_pct = abs(vol_delta) / max(prev_v, 1e-9)
                flow_dir = "IN" if vol_delta >= 0 else "OUT"
                vol_prev[key] = vol

                # ARB check (priority)
                side = _extract_side_prices(m)
                arb = find_arbitrage(side)

                if arb:
                    # short cooldown for arb
                    if now_ts - last_alert_ts.get(key, 0) < 180:
                        continue

                    news, nscore = get_relevant_news(title)
                    msg = (
                        f"🚨 ARB OPPORTUNITY\n"
                        f"🎯 ACTION: ENTER BOTH SIDES (ARBITRAGE)\n"
                        f"✅ Buy YES @ {arb['yes_ask']:.3f}  +  Buy NO @ {arb['no_ask']:.3f}\n"
                        f"🧠 Locked profit ≈ {(arb['locked_profit']*100):.2f}%  (sum={arb['sum']:.3f}, buffer={ARB_BUFFER:.3f})\n"
                        f"📊 Vol={vol:,.0f} | Liq={liq:,.0f}\n"
                        f"🗞 News (relevance score {nscore}):\n{news}\n"
                        f"📝 {title}\n{url}"
                    )
                    send_telegram(msg)
                    last_alert_ts[key] = now_ts
                    sent += 1
                    if sent >= 16:
                        break
                    continue

                # If price already basically resolved, skip (this fixes your “move 22% but now 0” complaint)
                if is_extreme_price(yes):
                    continue

                # Need at least 2 points for move detection
                if len(price_hist[key]) < 2:
                    continue

                prev_yes = price_hist[key][-2]
                abs_move = abs(yes - prev_yes)
                price_move_pct = abs_move / max(prev_yes, 1e-9)

                # Fresh-move trigger (last scan)
                move_trigger = (price_move_pct >= PRICE_MOVE_ALERT_PCT) or (abs_move >= ABS_PRICE_MOVE_ALERT)
                flow_trigger = (flow_move_pct >= FLOW_MOVE_ALERT_PCT)

                if not (move_trigger or flow_trigger):
                    continue

                # Avoid stale: require the move to be continuing OR flow is still strong
                continuing = is_continuing(price_hist[key])
                if not continuing and not flow_trigger:
                    continue

                # Avoid “spike then fade”: if already retraced, skip
                if retraced_too_much(price_hist[key]):
                    continue

                # Cooldown (opportunity-first but not spam)
                if now_ts - last_alert_ts.get(key, 0) < 420:
                    continue

                dir_label = move_direction(yes, prev_yes)
                rec = recommendation_from_direction(dir_label)

                # News: only include if relevant; otherwise honest label
                news, nscore = get_relevant_news(title)

                msg = (
                    f"🚨 FRESH MOVE OPPORTUNITY\n"
                    f"🎯 ACTION: {rec}\n"
                    f"🧠 Why NOW: PriceMove={_pct(price_move_pct)} (abs {abs_move:.3f}) | Flow={flow_dir} {_pct(flow_move_pct)} (Δ{abs(vol_delta):,.0f})\n"
                    f"💰 YES={yes:.3f} | NO≈{(1-yes):.3f} | Dir={dir_label}\n"
                    f"📊 Vol={vol:,.0f} | Liq={liq:,.0f}\n"
                    f"🗞 News (relevance score {nscore}):\n{news}\n"
                    f"📝 {title}\n{url}"
                )
                send_telegram(msg)
                last_alert_ts[key] = now_ts
                sent += 1
                if sent >= 16:
                    break

            except Exception:
                continue

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
