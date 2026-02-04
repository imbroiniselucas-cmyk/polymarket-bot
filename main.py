#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Aggressive Signal Bot (Telegram)

✅ More alerts (lower thresholds)
✅ “Silent accumulation” alerts (volume up, price flat)
✅ Clear action wording (BUY YES / BUY NO / WATCH)
✅ Anti-spam that still re-alerts if the signal strengthens

ENV VARS (Railway):
- TELEGRAM_BOT_TOKEN   (required)
- TELEGRAM_CHAT_ID     (required)
- POLY_ENDPOINT        (optional) -> URL returning JSON list of markets (your own endpoint)
- POLY_LIMIT           (default 120)
- LOOP_SECONDS         (default 35)

Aggressiveness:
- MIN_SCORE            (default 6.5)
- MIN_PRICE_DELTA_PCT  (default 0.6)
- MIN_VOL_DELTA        (default 300)
- MIN_LIQ              (default 8000)

Accumulation mode:
- ACC_VOL_DELTA        (default 600)
- ACC_MAX_PRICE_PCT    (default 0.25)

Anti-spam:
- COOLDOWN_MIN         (default 10)
- SCORE_BUMP_REALERT   (default 1.5)
- PRICE_BUMP_REALERT   (default 1.2)
"""

import os
import time
import json
import math
import requests
from datetime import datetime, timezone

# -------------------- CONFIG --------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()
POLY_LIMIT = int(os.getenv("POLY_LIMIT", "120"))
LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "35"))

MIN_SCORE = float(os.getenv("MIN_SCORE", "6.5"))
MIN_PRICE_DELTA_PCT = float(os.getenv("MIN_PRICE_DELTA_PCT", "0.6"))
MIN_VOL_DELTA = float(os.getenv("MIN_VOL_DELTA", "300"))
MIN_LIQ = float(os.getenv("MIN_LIQ", "8000"))

ACC_VOL_DELTA = float(os.getenv("ACC_VOL_DELTA", "600"))
ACC_MAX_PRICE_PCT = float(os.getenv("ACC_MAX_PRICE_PCT", "0.25"))

COOLDOWN_MIN = float(os.getenv("COOLDOWN_MIN", "10"))
SCORE_BUMP_REALERT = float(os.getenv("SCORE_BUMP_REALERT", "1.5"))
PRICE_BUMP_REALERT = float(os.getenv("PRICE_BUMP_REALERT", "1.2"))

STATE_FILE = "state.json"
TIMEOUT = 15

# -------------------- TELEGRAM --------------------
def tg_send(msg: str):
    if not TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        print(msg)
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=TIMEOUT).raise_for_status()
    except Exception as e:
        print("Telegram send error:", e)

# -------------------- STATE --------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"prev": {}, "sent": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"prev": {}, "sent": {}}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print("State save error:", e)

# -------------------- POLY FETCH --------------------
def _try_get(url, params=None):
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def fetch_markets():
    """
    Returns a LIST of market dicts.
    Tries:
      1) POLY_ENDPOINT if provided (must return list or {"markets": [...]})
      2) Common Polymarket Gamma API patterns (best-effort)
    """
    # 1) Custom endpoint (your own / known good)
    if POLY_ENDPOINT:
        data = _try_get(POLY_ENDPOINT)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for k in ("markets", "data", "results"):
                if k in data and isinstance(data[k], list):
                    return data[k]
        raise RuntimeError("POLY_ENDPOINT did not return a list of markets")

    # 2) Best-effort fallback (Gamma API style)
    # NOTE: This may change. If it fails, set POLY_ENDPOINT to your working JSON feed.
    fallback_urls = [
        ("https://gamma-api.polymarket.com/markets", {"active": "true", "closed": "false", "limit": str(POLY_LIMIT)}),
        ("https://gamma-api.polymarket.com/markets", {"limit": str(POLY_LIMIT)}),
    ]
    last_err = None
    for url, params in fallback_urls:
        try:
            data = _try_get(url, params=params)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for k in ("markets", "data", "results"):
                    if k in data and isinstance(data[k], list):
                        return data[k]
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Could not fetch markets. Set POLY_ENDPOINT. Last error: {last_err}")

# -------------------- PARSING HELPERS --------------------
def _get_id(m):
    return str(m.get("id") or m.get("marketId") or m.get("slug") or m.get("conditionId") or "")

def _get_title(m):
    return (m.get("question")
            or m.get("title")
            or m.get("name")
            or m.get("slug")
            or "Unknown market")

def _to_float(x, default=0.0):
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

def _get_volume(m):
    # Gamma often uses volumeNum; some feeds use volume / volume24hr / volumeUSD
    for k in ("volumeNum", "volume", "volumeUSD", "volume24hr", "volume24h", "totalVolume"):
        if k in m:
            return _to_float(m.get(k), 0.0)
    return 0.0

def _get_liquidity(m):
    for k in ("liquidityNum", "liquidity", "liquidityUSD", "totalLiquidity"):
        if k in m:
            return _to_float(m.get(k), 0.0)
    return 0.0

def _get_yes_price(m):
    """
    Tries multiple possible formats:
    - outcomePrices: ["0.41","0.59"] with outcomes ["Yes","No"]
    - outcomePrices: {"Yes":0.41,"No":0.59}
    - yesPrice / bestBid / bestAsk formats (fallback)
    """
    # dict form
    op = m.get("outcomePrices")
    outcomes = m.get("outcomes")

    if isinstance(op, dict):
        # keys may be "Yes"/"No" or "YES"/"NO"
        for ky in ("Yes", "YES", "yes"):
            if ky in op:
                return _to_float(op[ky], 0.0)

    # list form aligned with outcomes
    if isinstance(op, list) and isinstance(outcomes, list) and len(op) == len(outcomes):
        for i, name in enumerate(outcomes):
            if str(name).lower() == "yes":
                return _to_float(op[i], 0.0)

    # sometimes a direct yesPrice exists
    for k in ("yesPrice", "yes_price", "p_yes", "probYes"):
        if k in m:
            return _to_float(m.get(k), 0.0)

    return 0.0

def _market_url(m):
    # If feed already has url
    for k in ("url", "marketUrl", "link"):
        if m.get(k):
            return str(m.get(k))
    # try slug to build a best-effort link
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return "https://polymarket.com/"

# -------------------- SCORING / SIGNAL --------------------
def compute_score(vol_delta, price_delta_pct_abs, liq):
    # Aggressive but stable scoring (0–15-ish)
    # - price move matters early (x2)
    # - volume delta matters (log scaled)
    # - liquidity matters (log scaled)
    s_price = 2.0 * price_delta_pct_abs
    s_vol = 3.2 * math.log1p(max(0.0, vol_delta) / 100.0)
    s_liq = 2.2 * math.log1p(max(0.0, liq) / 10000.0)
    score = s_price + s_vol + s_liq
    return min(15.0, max(0.0, score))

def action_from_momentum(price_delta_pct, yes_price):
    """
    Heuristic:
    - if YES price rising -> momentum toward YES
    - if YES price falling -> momentum toward NO
    """
    if yes_price <= 0:
        return "WATCH", "Price not available"
    if price_delta_pct >= 0.3:
        return "CONSIDER BUY YES", "YES probability rising (momentum ↑)"
    if price_delta_pct <= -0.3:
        return "CONSIDER BUY NO", "YES probability falling (momentum ↓)"
    return "WATCH", "No clear momentum"

def level_emoji(score):
    if score >= 9.0:
        return "🔴 MUITO FORTE"
    if score >= 7.0:
        return "🟠 FORTE"
    return "🟡 AGRESSIVO"

def should_alert(now_ts, sent_meta, score, price_delta_pct_abs):
    """
    sent_meta: {"ts":..., "score":..., "price_abs":...}
    """
    if not sent_meta:
        return True

    last_ts = sent_meta.get("ts", 0)
    last_score = sent_meta.get("score", 0.0)
    last_price_abs = sent_meta.get("price_abs", 0.0)

    cooldown_ok = (now_ts - last_ts) >= (COOLDOWN_MIN * 60.0)
    strengthened = (score >= (last_score + SCORE_BUMP_REALERT)) or (price_delta_pct_abs >= (last_price_abs + PRICE_BUMP_REALERT))

    return cooldown_ok or strengthened

# -------------------- MAIN LOOP --------------------
def main():
    state = load_state()

    tg_send("🤖 Bot ligado (AGRESSIVO): sinais + acumulação silenciosa + anti-spam inteligente.")

    while True:
        try:
            markets = fetch_markets()
        except Exception as e:
            tg_send(f"⚠️ Erro ao buscar mercados: {e}\nDica: configure POLY_ENDPOINT com um JSON estável.")
            time.sleep(max(LOOP_SECONDS, 30))
            continue

        now_ts = time.time()
        prev_map = state.get("prev", {})
        sent_map = state.get("sent", {})

        alerts_sent = 0

        for m in markets:
            mid = _get_id(m)
            if not mid:
                continue

            title = _get_title(m)
            url = _market_url(m)

            vol = _get_volume(m)
            liq = _get_liquidity(m)
            yes_price = _get_yes_price(m)

            prev = prev_map.get(mid, {})
            prev_vol = _to_float(prev.get("vol"), vol)
            prev_price = _to_float(prev.get("yes_price"), yes_price)

            vol_delta = vol - prev_vol
            price_delta_pct = 0.0
            if prev_price > 0 and yes_price > 0:
                price_delta_pct = ((yes_price - prev_price) / prev_price) * 100.0

            price_delta_abs = abs(price_delta_pct)
            score = compute_score(vol_delta, price_delta_abs, liq)

            # Alert conditions
            meets_core = (score >= MIN_SCORE) and (liq >= MIN_LIQ) and (
                price_delta_abs >= MIN_PRICE_DELTA_PCT or vol_delta >= MIN_VOL_DELTA
            )

            meets_acc = (liq >= MIN_LIQ) and (vol_delta >= ACC_VOL_DELTA) and (price_delta_abs <= ACC_MAX_PRICE_PCT)

            if not (meets_core or meets_acc):
                # update prev snapshot and continue
                prev_map[mid] = {"vol": vol, "yes_price": yes_price, "ts": now_ts}
                continue

            # anti-spam logic
            sent_meta = sent_map.get(mid)
            if not should_alert(now_ts, sent_meta, score, price_delta_abs):
                prev_map[mid] = {"vol": vol, "yes_price": yes_price, "ts": now_ts}
                continue

            lvl = level_emoji(score)

            if meets_acc and not meets_core:
                action = "👀 WATCH / EARLY"
                why = "Silent accumulation (volume ↑ while price ~flat)"
            else:
                action, why = action_from_momentum(price_delta_pct, yes_price)

            # Compose message
            msg = (
                f"🚨 {lvl}\n"
                f"🎯 ACTION: {action}\n"
                f"🧠 Reason: {why}\n\n"
                f"• Market: {title}\n"
                f"• YES price: {yes_price:.3f}\n"
                f"• PriceΔ: {price_delta_pct:+.2f}% | VolΔ: {vol_delta:+.0f} | Liq: {liq:.0f}\n"
                f"• Score: {score:.2f}\n"
                f"{url}"
            )

            tg_send(msg)
            alerts_sent += 1

            # save sent meta
            sent_map[mid] = {"ts": now_ts, "score": score, "price_abs": price_delta_abs}

            # update prev snapshot
            prev_map[mid] = {"vol": vol, "yes_price": yes_price, "ts": now_ts}

            # soft cap per cycle to avoid floods (still aggressive)
            if alerts_sent >= 10:
                break

        state["prev"] = prev_map
        state["sent"] = sent_map
        save_state(state)

        time.sleep(LOOP_SECONDS)

if __name__ == "__main__":
    main()
