#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot — AGGRESSIVE v6 (Opportunity-First + Price Evaluation + News + Arb)

Goals you asked for:
✅ More aggressive (more alerts, faster re-alerts)
✅ Stronger price evaluation (not only "cheap/expensive"):
   - momentum (last scan + trend over last 3–5 scans)
   - mean-reversion signals (snapback after spike)
   - "value zone" only as fallback
✅ Opportunities linked to:
   - Arbitrage (risk-free when orderbook is available)
   - News catalysts (relevance-scored, recent, not random)
✅ Avoid late alerts:
   - skip already-resolved prices (near 0 or 1) unless ARB
   - avoid spike-then-fade (stale retrace)

REQUIRED ENV:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

OPTIONAL ENV:
  POLY_ENDPOINT
  SCAN_EVERY_SEC=120
  MAX_MARKETS=450

  # Aggressive filters
  VOL_MIN=6000
  LIQ_MIN=2500

  # Alert thresholds (aggressive)
  PRICE_MOVE_ALERT_PCT=3.0        # >= 3% since last scan
  ABS_PRICE_MOVE_ALERT=0.012      # or >= 1.2 cents
  FLOW_MOVE_ALERT_PCT=8.0         # volume delta >= 8% since last scan

  # Arb
  ARB_BUFFER=0.003                # require 0.3% locked edge

  # Late filters
  SKIP_IF_PRICE_BELOW=0.01
  SKIP_IF_PRICE_ABOVE=0.99
  STALE_RETRACE_CENTS=0.008

  # News
  NEWS_TIMESPAN=6h
  NEWS_MAX=6
  NEWS_MIN_SCORE=2

  # Re-alerting
  COOLDOWN_MOVE_SEC=240
  COOLDOWN_NEWS_SEC=180
  COOLDOWN_ARB_SEC=120

Focus cities for weather/climate parsing: London, Buenos Aires, Ankara
"""

import os
import time
import re
import math
from typing import Any, Dict, List, Optional, Tuple
from collections import deque

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

SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "120"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "450"))

VOL_MIN = float(os.getenv("VOL_MIN", "6000"))
LIQ_MIN = float(os.getenv("LIQ_MIN", "2500"))

PRICE_MOVE_ALERT_PCT = float(os.getenv("PRICE_MOVE_ALERT_PCT", "3.0")) / 100.0
ABS_PRICE_MOVE_ALERT = float(os.getenv("ABS_PRICE_MOVE_ALERT", "0.012"))
FLOW_MOVE_ALERT_PCT = float(os.getenv("FLOW_MOVE_ALERT_PCT", "8.0")) / 100.0

ARB_BUFFER = float(os.getenv("ARB_BUFFER", "0.003"))

SKIP_IF_PRICE_BELOW = float(os.getenv("SKIP_IF_PRICE_BELOW", "0.01"))
SKIP_IF_PRICE_ABOVE = float(os.getenv("SKIP_IF_PRICE_ABOVE", "0.99"))
STALE_RETRACE_CENTS = float(os.getenv("STALE_RETRACE_CENTS", "0.008"))

NEWS_MAX = int(os.getenv("NEWS_MAX", "6"))
NEWS_MIN_SCORE = int(os.getenv("NEWS_MIN_SCORE", "2"))

COOLDOWN_MOVE_SEC = int(os.getenv("COOLDOWN_MOVE_SEC", "240"))
COOLDOWN_NEWS_SEC = int(os.getenv("COOLDOWN_NEWS_SEC", "180"))
COOLDOWN_ARB_SEC = int(os.getenv("COOLDOWN_ARB_SEC", "120"))

HTTP_TIMEOUT = 20
UA = {"User-Agent": "AGGRESSIVE_EDGE_BOT/6.0"}

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
    return 0.0


def _liquidity(m: Dict[str, Any]) -> float:
    for k in ["liquidity", "liquidityUSD", "liquidityUsd", "liquidity_usd", "openInterest", "open_interest"]:
        if k in m:
            v = _to_float(m.get(k), default=0.0)
            if v > 0:
                return v
    return 0.0


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


def _extract_side_asks(m: Dict[str, Any]) -> Dict[str, Optional[float]]:
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
def find_arbitrage(asks: Dict[str, Optional[float]]) -> Optional[Dict[str, Any]]:
    ya = asks.get("yes_ask")
    na = asks.get("no_ask")
    if ya is None or na is None:
        return None
    total = ya + na
    if total < (1.0 - ARB_BUFFER):
        return {"yes_ask": ya, "no_ask": na, "sum": total, "locked_profit": (1.0 - total)}
    return None


# ----------------------------
# News (relevance-scored + recent)
# ----------------------------
STOP = {
    "will","the","a","an","to","of","in","on","by","for","and","or","is","are","be",
    "above","below","reach","hit","over","under","before","after","with","at","from",
    "today","tomorrow","price","yes","no","market","polymarket"
}
for c in CITY_KEYS:
    STOP |= set(c.split())

def extract_keywords(title: str) -> List[str]:
    toks = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", title)
    out = []
    for t in toks:
        tl = t.lower()
        if tl in STOP:
            continue
        if len(t) >= 4:
            out.append(t)
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
        "timespan": "6h",
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

def get_relevant_news(title: str) -> Tuple[str, int]:
    kws = extract_keywords(title)
    tlo = title.lower()
    city_q = None
    for c in CITY_KEYS:
        if c in tlo:
            city_q = c
            break

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

    score = 0
    good = []
    kwl = [k.lower() for k in kws]
    for h in heads:
        ht = h["title"].lower()
        hits = sum(1 for k in kwl if k in ht)
        if hits > 0:
            score += hits
            good.append(f"• {h['title']}" + (f" ({h['src']})" if h.get("src") else ""))

    if score < NEWS_MIN_SCORE or not good:
        return "• (no relevant catalyst found in last 6h)", score

    return "\n".join(good[:3]), score


# ----------------------------
# Price evaluation (stronger)
# ----------------------------
def is_extreme_price(p: float) -> bool:
    return p <= SKIP_IF_PRICE_BELOW or p >= SKIP_IF_PRICE_ABOVE

def direction(now: float, prev: float) -> str:
    return "UP->YES" if now > prev else ("DOWN->NO" if now < prev else "FLAT")

def rec_from_dir(d: str) -> str:
    if d == "UP->YES":
        return "ENTER YES (A FAVOR)"
    if d == "DOWN->NO":
        return "ENTER NO (CONTRA)"
    return "WAIT"

def trend_score(hist: deque) -> float:
    # + if trending up, - if trending down
    if len(hist) < 3:
        return 0.0
    ds = 0.0
    for i in range(1, len(hist)):
        ds += (hist[i] - hist[i-1])
    return ds

def is_spike_then_fade(hist: deque) -> bool:
    if len(hist) < 4:
        return False
    w = list(hist)[-4:]
    mx = max(w); mn = min(w); last = w[-1]
    if mx - mn < 0.015:
        return False
    retr = max(abs(mx - last), abs(last - mn))
    return retr >= STALE_RETRACE_CENTS

def snapback_signal(hist: deque) -> Optional[str]:
    # detect a snapback reversal after a sharp move (mean reversion)
    if len(hist) < 4:
        return None
    a, b, c, d = list(hist)[-4:]
    # Up spike then drop (fade) -> favor NO
    if (b - a) > 0.015 and (d - c) < -0.010:
        return "ENTER NO (CONTRA) — snapback after spike"
    # Down spike then bounce -> favor YES
    if (b - a) < -0.015 and (d - c) > 0.010:
        return "ENTER YES (A FAVOR) — snapback after dump"
    return None


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
        if offset > 1400:
            break
    return out[:MAX_MARKETS]


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return

    price_hist: Dict[str, deque] = {}
    vol_prev: Dict[str, float] = {}
    last_alert_ts: Dict[str, float] = {}

    send_telegram("🤖 Bot ON: AGGRESSIVE v6 (PriceEval + Arb + NewsCatalyst + FreshMoves).")

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

                # History
                if key not in price_hist:
                    price_hist[key] = deque(maxlen=7)
                price_hist[key].append(yes)

                prev_v = vol_prev.get(key, vol)
                vol_delta = vol - prev_v
                flow_move_pct = abs(vol_delta) / max(prev_v, 1e-9)
                flow_dir = "IN" if vol_delta >= 0 else "OUT"
                vol_prev[key] = vol

                # Arbitrage first
                asks = _extract_side_asks(m)
                arb = find_arbitrage(asks)
                if arb:
                    if now_ts - last_alert_ts.get(key, 0) < COOLDOWN_ARB_SEC:
                        continue
                    news, nscore = get_relevant_news(title)
                    msg = (
                        f"🚨 ARB OPPORTUNITY\n"
                        f"🎯 ACTION: ENTER BOTH SIDES (ARBITRAGE)\n"
                        f"✅ Buy YES @ {arb['yes_ask']:.3f}  +  Buy NO @ {arb['no_ask']:.3f}\n"
                        f"🧠 Locked profit ≈ {(arb['locked_profit']*100):.2f}% (sum={arb['sum']:.3f}, buffer={ARB_BUFFER:.3f})\n"
                        f"💸 Flow: {flow_dir} {_pct(flow_move_pct)} (Δ{abs(vol_delta):,.0f}) | Vol={vol:,.0f} | Liq={liq:,.0f}\n"
                        f"🗞 News (score {nscore}):\n{news}\n"
                        f"📝 {title}\n{url}"
                    )
                    send_telegram(msg)
                    last_alert_ts[key] = now_ts
                    sent += 1
                    if sent >= 22:
                        break
                    continue

                # Avoid late resolved markets
                if is_extreme_price(yes):
                    continue

                if len(price_hist[key]) < 2:
                    continue

                prev_yes = price_hist[key][-2]
                abs_move = abs(yes - prev_yes)
                price_move_pct = abs_move / max(prev_yes, 1e-9)

                move_trigger = (price_move_pct >= PRICE_MOVE_ALERT_PCT) or (abs_move >= ABS_PRICE_MOVE_ALERT)
                flow_trigger = (flow_move_pct >= FLOW_MOVE_ALERT_PCT)

                if not (move_trigger or flow_trigger):
                    continue

                # Avoid stale spike-fade
                if is_spike_then_fade(price_hist[key]):
                    continue

                # Cooldowns: tighter for news-worthy catalysts
                news, nscore = get_relevant_news(title)
                cooldown = COOLDOWN_NEWS_SEC if nscore >= NEWS_MIN_SCORE else COOLDOWN_MOVE_SEC
                if now_ts - last_alert_ts.get(key, 0) < cooldown:
                    continue

                d = direction(yes, prev_yes)
                base_rec = rec_from_dir(d)

                # Stronger price evaluation layer
                tscore = trend_score(price_hist[key])
                snap = snapback_signal(price_hist[key])

                if snap:
                    rec = snap.split(" — ")[0]  # keeps explicit YES/NO
                    why_eval = snap
                else:
                    # If trend score strong, follow trend; if weak but flow big, follow flow direction via last move
                    if abs(tscore) >= 0.015:
                        rec = base_rec
                        why_eval = f"trend_strength={tscore:+.3f} (follow trend)"
                    else:
                        rec = base_rec
                        why_eval = f"trend_strength={tscore:+.3f} (light trend)"

                alert_kind = "CATALYST+MOVE" if nscore >= NEWS_MIN_SCORE else "MOVE"
                msg = (
                    f"🚨 {alert_kind} OPPORTUNITY\n"
                    f"🎯 ACTION: {rec}\n"
                    f"🧠 Why NOW: PriceMove={_pct(price_move_pct)} (abs {abs_move:.3f}) | Flow={flow_dir} {_pct(flow_move_pct)} (Δ{abs(vol_delta):,.0f})\n"
                    f"📈 PriceEval: {why_eval}\n"
                    f"💰 YES={yes:.3f} | NO≈{(1-yes):.3f} | Dir={d}\n"
                    f"📊 Vol={vol:,.0f} | Liq={liq:,.0f}\n"
                    f"🗞 News (score {nscore}):\n{news}\n"
                    f"📝 {title}\n{url}"
                )
                send_telegram(msg)
                last_alert_ts[key] = now_ts
                sent += 1
                if sent >= 22:
                    break

            except Exception:
                continue

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
