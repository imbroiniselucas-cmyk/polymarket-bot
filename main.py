#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Opportunity Bot (Telegram)
- Only sends BUY (entry) alerts
- Only sends alerts with score >= MIN_SCORE (default 35)
- Designed to be "quiet" and actionable

ENV required:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

Optional ENV:
  MIN_SCORE=35
  POLL_SECONDS=300
  MAX_ALERTS_PER_CYCLE=4
  COOLDOWN_MINUTES=90
  MIN_LIQ=0
  MIN_VOL24H=0
  SPREAD_MAX=0.08
  EDGE_MIN=0.012
  MOMENTUM_MIN=0.006
  MOVE_VOL_MIN=2500

Data source (choose one):
  1) POLY_ENDPOINT -> URL returning a JSON list of markets (your existing endpoint)
     Each market item ideally contains:
       id, slug or url, question/title, outcomes/prices, volume, liquidity, volume24h, lastPrice/price history, etc.
  If your previous bot already had a working fetcher, paste your endpoint here (recommended).
"""

import os
import time
import json
import math
import hashlib
from datetime import datetime, timezone, timedelta

import requests

# ==========================
# ENV CONFIG
# ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()  # your JSON endpoint
MIN_SCORE = float(os.getenv("MIN_SCORE", "35"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))  # 5 min
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "4"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "90"))

# light “sanity” filters (kept flexible by default)
MIN_LIQ = float(os.getenv("MIN_LIQ", "0"))
MIN_VOL24H = float(os.getenv("MIN_VOL24H", "0"))

# microstructure / entry quality
SPREAD_MAX = float(os.getenv("SPREAD_MAX", "0.08"))     # 8% max implied spread (avoid huge trap)
EDGE_MIN = float(os.getenv("EDGE_MIN", "0.012"))        # min edge vs mid ~ 1.2%
MOMENTUM_MIN = float(os.getenv("MOMENTUM_MIN", "0.006"))# price move threshold ~ 0.6%
MOVE_VOL_MIN = float(os.getenv("MOVE_VOL_MIN", "2500")) # volume delta threshold

# always only BUY alerts
ONLY_BUY = True

# ==========================
# TELEGRAM
# ==========================
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:300])
    except Exception as e:
        print("Telegram exception:", repr(e))

# ==========================
# FETCH MARKETS
# ==========================
def fetch_markets() -> list[dict]:
    """
    Expect POLY_ENDPOINT to return a JSON list of markets.
    If your endpoint returns {"markets":[...]}, we handle that too.
    """
    if not POLY_ENDPOINT:
        raise RuntimeError("POLY_ENDPOINT not set. Provide your working JSON endpoint used before.")

    r = requests.get(POLY_ENDPOINT, timeout=30)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
        return data["markets"]
    if isinstance(data, list):
        return data
    raise RuntimeError("Unexpected POLY_ENDPOINT response format (expected list or {'markets': [...]})")

# ==========================
# HELPERS: MARKET NORMALIZATION
# ==========================
def _get(m: dict, *keys, default=None):
    for k in keys:
        if k in m and m[k] is not None:
            return m[k]
    return default

def market_url(m: dict) -> str:
    return _get(m, "url", "marketUrl", "link", default="").strip()

def market_title(m: dict) -> str:
    return _get(m, "question", "title", "name", default="(untitled)").strip()

def market_id(m: dict) -> str:
    mid = _get(m, "id", "marketId", "slug", default="")
    if mid:
        return str(mid)
    # fallback stable hash
    base = (market_url(m) or market_title(m)).encode("utf-8", "ignore")
    return hashlib.sha1(base).hexdigest()[:12]

def get_liq(m: dict) -> float:
    return float(_get(m, "liquidity", "liq", "liquidityUSD", default=0) or 0)

def get_vol24h(m: dict) -> float:
    return float(_get(m, "volume24h", "vol24h", "volume_24h", default=0) or 0)

def get_volume(m: dict) -> float:
    return float(_get(m, "volume", "vol", "volumeUSD", default=0) or 0)

def get_last_price_yes(m: dict) -> float | None:
    """
    Tries to locate a YES probability/price in [0,1].
    Supports a few common formats:
      - m["yes"] or m["yesPrice"]
      - m["outcomes"] = [{"name":"Yes","price":0.42}, ...]
      - m["prices"] = {"YES":0.42,"NO":0.58}
    """
    direct = _get(m, "yes", "yesPrice", "p_yes", default=None)
    if direct is not None:
        try:
            p = float(direct)
            return p if 0 <= p <= 1 else None
        except:
            pass

    outcomes = _get(m, "outcomes", "tokens", default=None)
    if isinstance(outcomes, list):
        for o in outcomes:
            name = str(_get(o, "name", "outcome", default="")).lower()
            if name in ("yes", "y"):
                try:
                    p = float(_get(o, "price", "prob", "p", default=None))
                    return p if p is not None and 0 <= p <= 1 else None
                except:
                    return None

    prices = _get(m, "prices", "price", default=None)
    if isinstance(prices, dict):
        for k in prices.keys():
            if str(k).lower() == "yes":
                try:
                    p = float(prices[k])
                    return p if 0 <= p <= 1 else None
                except:
                    return None

    return None

def get_best_bid_ask_yes(m: dict) -> tuple[float | None, float | None]:
    """
    Tries to read bid/ask for YES if present.
    Common shapes:
      - m["orderbook"]["yes"]["bid"], ["ask"]
      - m["bestBidYes"], m["bestAskYes"]
    """
    bid = _get(m, "bestBidYes", "bidYes", default=None)
    ask = _get(m, "bestAskYes", "askYes", default=None)
    if bid is not None or ask is not None:
        try:
            bid = float(bid) if bid is not None else None
            ask = float(ask) if ask is not None else None
            return bid, ask
        except:
            return None, None

    ob = _get(m, "orderbook", "orderBook", default=None)
    if isinstance(ob, dict):
        yes_ob = None
        for k in ob.keys():
            if str(k).lower() == "yes":
                yes_ob = ob[k]
                break
        if isinstance(yes_ob, dict):
            try:
                bid = yes_ob.get("bid") or yes_ob.get("bestBid")
                ask = yes_ob.get("ask") or yes_ob.get("bestAsk")
                bid = float(bid) if bid is not None else None
                ask = float(ask) if ask is not None else None
                return bid, ask
            except:
                return None, None

    return None, None

# ==========================
# SCORING (only for BUY entries)
# ==========================
def clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))

def score_market(m: dict, prev: dict | None) -> tuple[float, dict]:
    """
    Returns (score, details) for BUY opportunities.
    The score emphasizes:
      - edge vs mid (if bid/ask exist) or vs last price proxy
      - momentum / move
      - liquidity + volume changes
      - penalize large spreads (spread trap)
    """
    liq = get_liq(m)
    vol = get_volume(m)
    vol24 = get_vol24h(m)
    p_yes = get_last_price_yes(m)

    bid, ask = get_best_bid_ask_yes(m)
    if bid is not None and ask is not None and ask > 0:
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / max(mid, 1e-9)
    else:
        mid = p_yes if p_yes is not None else None
        spread = None

    # deltas
    prev_vol = float(prev.get("volume", 0)) if prev else 0.0
    prev_p = float(prev.get("p_yes", p_yes if p_yes is not None else 0.0)) if prev else (p_yes or 0.0)

    vol_delta = max(0.0, vol - prev_vol)
    price_move = abs((p_yes or 0.0) - prev_p)

    # entry direction (BUY) logic:
    # We consider BUY when either:
    #  - bid/ask present and "edge" positive (bid below mid by some margin) i.e. you can buy near bid/ask cheaply
    #  - or momentum is positive (p_yes rising) and move/volume confirm
    #
    # Since Polymarket is binary, “BUY” here means "enter the favored side now" (we’ll specify YES/NO).
    # Determine suggested side by where probability is moving:
    suggested = None  # "YES" or "NO"
    if p_yes is not None:
        # if p_yes increasing -> YES momentum, else NO momentum
        suggested = "YES" if (p_yes - prev_p) >= 0 else "NO"

    # edge proxy:
    # If orderbook exists, reward tighter spread and ability to buy near bid/ask.
    edge = 0.0
    spread_penalty = 0.0
    if bid is not None and ask is not None and mid is not None:
        # "cheapness": if mid is low for suggested side, that can be "value",
        # but we mainly use spread + mid distance to avoid traps.
        # We'll approximate edge as tightness + move+vol confirmation.
        edge = max(0.0, (SPREAD_MAX - spread)) if spread is not None else 0.0
        if spread is not None and spread > SPREAD_MAX:
            spread_penalty = (spread - SPREAD_MAX) * 30.0  # big penalty
    else:
        # no orderbook -> edge from momentum + vol_delta only
        edge = 0.0

    # components normalized (0..1-ish)
    liq_s = clamp(math.log10(liq + 1) / 6.0, 0, 1)          # 1 around ~1M
    vol24_s = clamp(math.log10(vol24 + 1) / 6.0, 0, 1)
    vdelta_s = clamp(math.log10(vol_delta + 1) / 5.0, 0, 1) # 1 around ~100k
    move_s = clamp(price_move / 0.05, 0, 1)                 # 5% prob move hits 1
    edge_s = clamp(edge / SPREAD_MAX, 0, 1) if SPREAD_MAX > 0 else 0.0

    # base score (0..100 approx)
    score = 100.0 * (
        0.28 * vdelta_s +
        0.22 * move_s +
        0.18 * liq_s +
        0.12 * vol24_s +
        0.20 * edge_s
    ) - spread_penalty

    # enforce minimum “actionability”
    action_ok = True
    reasons = []
    if liq < MIN_LIQ:
        action_ok = False
        reasons.append(f"liq<{MIN_LIQ:g}")
    if vol24 < MIN_VOL24H:
        action_ok = False
        reasons.append(f"vol24h<{MIN_VOL24H:g}")
    if vol_delta < MOVE_VOL_MIN and price_move < MOMENTUM_MIN:
        action_ok = False
        reasons.append("weak move/flow")
    if spread is not None and spread > SPREAD_MAX:
        action_ok = False
        reasons.append("spread trap")
    # edge min if spread exists
    if spread is not None and (SPREAD_MAX - spread) < EDGE_MIN:
        action_ok = False
        reasons.append("low edge")

    details = {
        "liq": liq,
        "vol": vol,
        "vol24h": vol24,
        "vol_delta": vol_delta,
        "p_yes": p_yes,
        "prev_p": prev_p,
        "price_move": price_move,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "suggested": suggested,
        "action_ok": action_ok,
        "blocks": reasons,
        "url": market_url(m),
        "title": market_title(m),
        "id": market_id(m),
    }

    if not action_ok:
        # do not send at all if not actionable; score becomes 0 for filtering
        return 0.0, details

    return max(0.0, score), details

# ==========================
# STATE (cooldowns)
# ==========================
STATE_FILE = "state.json"

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"seen": {}, "snap": {}}

def save_state(st: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("state save error:", repr(e))

def now_ts() -> int:
    return int(time.time())

def can_alert(st: dict, mid: str, score: float) -> bool:
    """
    Cooldown per market; only alert again after COOLDOWN_MINUTES
    or if score jumps materially.
    """
    seen = st.get("seen", {}).get(mid)
    if not seen:
        return True
    last_ts = int(seen.get("ts", 0))
    last_score = float(seen.get("score", 0))
    if now_ts() - last_ts >= COOLDOWN_MINUTES * 60:
        return True
    # if score improves a lot, allow earlier alert
    if score >= last_score + 12:
        return True
    return False

def mark_alerted(st: dict, mid: str, score: float) -> None:
    st.setdefault("seen", {})[mid] = {"ts": now_ts(), "score": round(score, 2)}

# ==========================
# MESSAGE FORMAT (BUY ONLY)
# ==========================
def fmt_pct(x: float | None) -> str:
    if x is None:
        return "-"
    return f"{x*100:.2f}%"

def format_buy_alert(score: float, d: dict) -> str:
    title = d["title"]
    url = d["url"] or ""
    suggested = d.get("suggested") or "YES"

    # Make it explicit: BUY YES / BUY NO
    action_line = f"🟢 <b>BUY {suggested}</b> (entry)"
    # numeric context
    vol_delta = d["vol_delta"]
    liq = d["liq"]
    p_yes = d["p_yes"]
    prev_p = d["prev_p"]
    pmove = d["price_move"]

    bid = d.get("bid")
    ask = d.get("ask")
    spread = d.get("spread")

    parts = []
    parts.append(f"🚨 <b>OPPORTUNITY</b> | Score: <b>{score:.1f}</b>")
    parts.append(action_line)
    parts.append(f"📝 <b>{title}</b>")
    if p_yes is not None:
        parts.append(f"📌 YES: <b>{p_yes:.3f}</b> (prev {prev_p:.3f}) | Move: <b>{pmove*100:.2f}%</b>")
    if bid is not None and ask is not None:
        parts.append(f"📚 Book YES | bid {bid:.3f} / ask {ask:.3f} | spread {fmt_pct(spread)}")
    parts.append(f"💧 Liq: <b>{liq:,.0f}</b> | VolΔ: <b>{vol_delta:,.0f}</b>")
    parts.append(f"🔎 Reason: move+flow confirmed, spread ok, actionable entry")
    if url:
        parts.append(url)
    return "\n".join(parts)

# ==========================
# MAIN LOOP
# ==========================
def main():
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
    st = load_state()
    snap = st.get("snap", {})  # store minimal previous snapshot per market

    print("BOOT_OK: main.py running")
    print(f"Config: MIN_SCORE={MIN_SCORE} POLL_SECONDS={POLL_SECONDS} ONLY_BUY={ONLY_BUY}")

    while True:
        try:
            markets = fetch_markets()

            scored = []
            for m in markets:
                mid = market_id(m)

                prev = snap.get(mid, None)
                score, details = score_market(m, prev)

                # update snapshot regardless
                snap[mid] = {
                    "volume": get_volume(m),
                    "p_yes": get_last_price_yes(m) or (prev.get("p_yes") if prev else 0.0),
                    "ts": now_ts(),
                }

                # Filter: BUY only + MIN_SCORE
                if score >= MIN_SCORE:
                    if ONLY_BUY:
                        # we already only produce actionable BUY; but keep check:
                        if details.get("action_ok"):
                            scored.append((score, details))

            # sort by best score
            scored.sort(key=lambda x: x[0], reverse=True)

            alerts_sent = 0
            for score, d in scored:
                if alerts_sent >= MAX_ALERTS_PER_CYCLE:
                    break

                mid = d["id"]
                if not can_alert(st, mid, score):
                    continue

                msg = format_buy_alert(score, d)
                tg_send(msg)

                mark_alerted(st, mid, score)
                alerts_sent += 1

            # persist state
            st["snap"] = snap
            save_state(st)

        except Exception as e:
            print("loop error:", repr(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
