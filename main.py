#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Alert Bot — Aggressive BUT Spread-Safe

Goals:
✅ Many alerts every 5–10 min
✅ Avoid "fake profit" situations caused by huge spread (bad exit)
✅ Clear recommendation: BUY YES / BUY NO / BUY BOTH (arb-ish) / WATCH / AVOID
✅ Force-send TopN picks every loop so it won't stay silent

Why spread filter matters:
Polymarket UI often shows midpoint/last-trade; if spread is large, your SELL hits bestBid which can be far lower. :contentReference[oaicite:1]{index=1}

REQUIRED ENV (Railway Variables):
- TELEGRAM_TOKEN=xxxx
- TELEGRAM_CHAT_ID=yyyy

Recommended aggressive defaults (can override via ENV):
- LOOP_SECONDS=300          (5 min)  OR  600 (10 min)
- MAX_PAGES=10              (markets pagination pages)
- PAGE_LIMIT=200            (items per page)
- MIN_LIQ=300               (very flexible)
- MIN_VOL24H=100            (very flexible)
- MAX_SPREAD=0.08           (HARD FILTER: skip if spread > 8 cents)
- MIN_BID=0.02              (skip if bestBid is basically dead)
- CHEAP_MAX=0.15
- EXPENSIVE_MIN=0.90
- ARB_EDGE=0.008
- MOVE_PCT_ALERT=0.8
- VOL_DELTA_ALERT=1500
- COOLDOWN_MIN=8
- ALWAYS_SEND_TOPN=6
- SEND_DIAGNOSTIC=1
"""

import os
import time
import json
import math
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests


# ------------------------ ENV / Config ------------------------
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "300"))  # 300=5min, 600=10min

# We'll scan markets directly (better fields: bestBid/bestAsk/spread)
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

MAX_PAGES = int(os.getenv("MAX_PAGES", "10"))
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "200"))

MIN_LIQ = float(os.getenv("MIN_LIQ", "300"))
MIN_VOL24H = float(os.getenv("MIN_VOL24H", "100"))

# SPREAD SAFETY (key part)
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.08"))  # 8 cents
MIN_BID = float(os.getenv("MIN_BID", "0.02"))        # if bid < 2c, exit is trash

CHEAP_MAX = float(os.getenv("CHEAP_MAX", "0.15"))
EXPENSIVE_MIN = float(os.getenv("EXPENSIVE_MIN", "0.90"))
ARB_EDGE = float(os.getenv("ARB_EDGE", "0.008"))

MOVE_PCT_ALERT = float(os.getenv("MOVE_PCT_ALERT", "0.8"))
VOL_DELTA_ALERT = float(os.getenv("VOL_DELTA_ALERT", "1500"))

COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "8"))
ALWAYS_SEND_TOPN = int(os.getenv("ALWAYS_SEND_TOPN", "6"))
SEND_DIAGNOSTIC = os.getenv("SEND_DIAGNOSTIC", "1").strip() not in ("0", "false", "False", "")

HTTP_TIMEOUT = 20
STATE_FILE = "state.json"

PM_MARKET_BASE = "https://polymarket.com/market/"


# ------------------------ Helpers ------------------------
def now_ts() -> int:
    return int(time.time())


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def safe_float(x: Any, default: float = 0.0) -> float:
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


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"seen": {}, "cooldown": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}, "cooldown": {}}


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


def tg_send(msg: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        print(msg)
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code >= 300:
            print("Telegram error:", r.status_code, r.text[:400])
    except Exception as e:
        print("Telegram exception:", repr(e))


def market_url(m: Dict[str, Any]) -> str:
    slug = (m.get("slug") or "").strip()
    return PM_MARKET_BASE + slug if slug else "(no slug)"


def parse_yes_no_prices(m: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Gamma provides outcomes + outcomePrices as JSON strings sometimes.
    We still parse them to get YES/NO "display" prices.
    """
    outcomes = m.get("outcomes")
    prices = m.get("outcomePrices")
    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
    except Exception:
        pass

    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) < 2 or len(prices) < 2:
        return None

    mapping = {}
    for o, p in zip(outcomes, prices):
        mapping[str(o).strip().lower()] = safe_float(p, 0.0)

    yes = mapping.get("yes")
    no = mapping.get("no")

    if yes is None or no is None:
        yes = safe_float(prices[0], 0.0)
        no = safe_float(prices[1], 0.0)

    return clamp(yes, 0.0, 1.0), clamp(no, 0.0, 1.0)


def fetch_markets_page(offset: int, limit: int) -> List[Dict[str, Any]]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
        # ordering by 24h volume keeps the scan relevant
        "order": "volume24hr",
        "ascending": "false",
    }
    r = requests.get(GAMMA_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


# ------------------------ Spread-safe checks ------------------------
def spread_fields(m: Dict[str, Any]) -> Tuple[float, float, float]:
    """
    Returns (best_bid, best_ask, spread)
    Gamma docs show these fields exist. :contentReference[oaicite:2]{index=2}
    """
    bid = safe_float(m.get("bestBid"), 0.0)
    ask = safe_float(m.get("bestAsk"), 0.0)
    spr = safe_float(m.get("spread"), 0.0)
    # if spread missing, compute if possible
    if spr <= 0 and bid > 0 and ask > 0:
        spr = max(0.0, ask - bid)
    return bid, ask, spr


def pass_spread_safety(m: Dict[str, Any]) -> Tuple[bool, str]:
    bid, ask, spr = spread_fields(m)

    if bid <= 0 or ask <= 0:
        return False, "no bid/ask"
    if bid < MIN_BID:
        return False, f"bid too low ({bid:.3f})"
    if spr > MAX_SPREAD:
        return False, f"spread too wide ({spr:.3f} > {MAX_SPREAD:.2f})"
    return True, f"spread ok ({spr:.3f})"


# ------------------------ Scoring / Recommendation ------------------------
def compute_score(
    yes: float, no: float,
    liq: float, vol24: float,
    move_pct: float, vol_delta: float,
    arb_gap: float,
    bid: float, ask: float, spr: float
) -> float:
    cheap_side = min(yes, no)
    cheapness = 0.0
    if cheap_side <= CHEAP_MAX:
        cheapness = (CHEAP_MAX - cheap_side) / max(CHEAP_MAX, 1e-9)

    arb_component = clamp(arb_gap / max(ARB_EDGE, 1e-9), 0.0, 2.5)
    move_component = clamp(abs(move_pct) / 5.0, 0.0, 2.0)
    vol_component = clamp(vol_delta / max(VOL_DELTA_ALERT, 1.0), 0.0, 2.5)

    # reward tighter spreads (safer exit)
    spread_bonus = clamp((MAX_SPREAD - spr) / max(MAX_SPREAD, 1e-9), 0.0, 1.0)  # 0..1

    # very light quality weighting (still aggressive)
    quality = clamp(math.log10(max(liq, 1.0)) / 6.0, 0.0, 1.0)
    activity = clamp(math.log10(max(vol24, 1.0)) / 7.0, 0.0, 1.0)

    raw = (
        30.0 * cheapness +
        28.0 * arb_component +
        17.0 * move_component +
        17.0 * vol_component +
        6.0 * spread_bonus +
        1.0 * quality +
        1.0 * activity
    )
    return clamp(raw, 0.0, 100.0)


def recommendation(yes: float, no: float, arb_gap: float) -> Tuple[str, str]:
    if arb_gap >= ARB_EDGE:
        return ("✅ ACTION: ARB-ISH → BUY BOTH (YES + NO) now", f"YES+NO={yes+no:.3f} (gap={arb_gap:.3f})")
    if yes <= CHEAP_MAX:
        return ("✅ ACTION: BUY YES now (cheap)", f"YES={yes:.3f} ≤ {CHEAP_MAX:.2f}")
    if no <= CHEAP_MAX:
        return ("✅ ACTION: BUY NO now (cheap)", f"NO={no:.3f} ≤ {CHEAP_MAX:.2f}")
    if yes >= EXPENSIVE_MIN:
        return ("⚠️ ACTION: AVOID YES (expensive) → prefer NO / wait", f"YES={yes:.3f} ≥ {EXPENSIVE_MIN:.2f}")
    if no >= EXPENSIVE_MIN:
        return ("⚠️ ACTION: AVOID NO (expensive) → prefer YES / wait", f"NO={no:.3f} ≥ {EXPENSIVE_MIN:.2f}")
    return ("👀 ACTION: WATCH", "no cheap/arb/overpriced signal")


def should_alert(score: float, move_pct: float, vol_delta: float, arb_gap: float, yes: float, no: float) -> bool:
    if score >= 45:
        return True
    if arb_gap >= ARB_EDGE:
        return True
    if yes <= CHEAP_MAX or no <= CHEAP_MAX:
        return True
    if abs(move_pct) >= MOVE_PCT_ALERT:
        return True
    if vol_delta >= VOL_DELTA_ALERT:
        return True
    return False


def cooldown_ok(state: Dict[str, Any], key: str, score: float) -> bool:
    last = int(state.setdefault("cooldown", {}).get(key, 0) or 0)
    if score >= 80:
        return True
    return (now_ts() - last) >= (COOLDOWN_MIN * 60)


def set_cooldown(state: Dict[str, Any], key: str) -> None:
    state.setdefault("cooldown", {})[key] = now_ts()


# ------------------------ Scan Loop ------------------------
def scan_once(state: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    """
    Returns: sent, scanned, eligible, spread_filtered_out, errors
    """
    errors = 0
    scanned = 0
    eligible = 0
    spread_out = 0
    sent = 0

    seen = state.setdefault("seen", {})

    markets: List[Dict[str, Any]] = []
    for page in range(MAX_PAGES):
        offset = page * PAGE_LIMIT
        try:
            markets.extend(fetch_markets_page(offset=offset, limit=PAGE_LIMIT))
        except Exception as e:
            errors += 1
            print("fetch markets error:", repr(e))
            break

    candidates = []  # (score, key, market, ...)

    for m in markets:
        scanned += 1

        liq = safe_float(m.get("liquidityNum"), safe_float(m.get("liquidity"), 0.0))
        vol24 = safe_float(m.get("volume24hr"), 0.0)

        if liq < MIN_LIQ or vol24 < MIN_VOL24H:
            continue

        # spread safety filter
        ok_spread, spread_reason = pass_spread_safety(m)
        if not ok_spread:
            spread_out += 1
            continue

        yn = parse_yes_no_prices(m)
        if not yn:
            continue
        yes, no = yn

        bid, ask, spr = spread_fields(m)

        key = str(m.get("conditionId") or m.get("id") or m.get("slug") or "")
        if not key:
            continue

        prev = seen.get(key, {})
        prev_yes = safe_float(prev.get("yes"), yes)
        prev_vol24 = safe_float(prev.get("vol24"), vol24)

        move_pct = ((yes - prev_yes) / prev_yes * 100.0) if prev_yes > 0 else 0.0
        vol_delta = vol24 - prev_vol24
        arb_gap = max(0.0, 1.0 - (yes + no))

        score = compute_score(yes, no, liq, vol24, move_pct, vol_delta, arb_gap, bid, ask, spr)
        candidates.append((score, key, m, yes, no, liq, vol24, move_pct, vol_delta, arb_gap, bid, ask, spr))

        eligible += 1

        if should_alert(score, move_pct, vol_delta, arb_gap, yes, no) and cooldown_ok(state, key, score):
            action, hint = recommendation(yes, no, arb_gap)
            title = (m.get("question") or "Market").strip()
            cat = (m.get("category") or "—").strip()
            url = market_url(m)

            msg = (
                f"🚨 ALERT | Score={score:.1f}\n"
                f"{action}\n"
                f"🧠 Reason: {hint} | Spread={spr:.3f} (bid={bid:.3f}/ask={ask:.3f})\n"
                f"📊 Move={move_pct:+.2f}% | VolΔ={vol_delta:+.0f} | Liq={liq:.0f} | Vol24h={vol24:.0f}\n"
                f"📌 {title}\n"
                f"🏷️ {cat}\n"
                f"🔗 {url}"
            )
            tg_send(msg)
            set_cooldown(state, key)
            sent += 1

        seen[key] = {"yes": yes, "no": no, "vol24": vol24, "liq": liq, "ts": now_ts()}

    # Force-send TopN every loop (but still spread-safe because candidates already filtered)
    if ALWAYS_SEND_TOPN > 0 and candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        forced = 0
        for (score, key, m, yes, no, liq, vol24, move_pct, vol_delta, arb_gap, bid, ask, spr) in candidates:
            if forced >= ALWAYS_SEND_TOPN:
                break
            if not cooldown_ok(state, key, score) and score < 85:
                continue

            action, hint = recommendation(yes, no, arb_gap)
            title = (m.get("question") or "Market").strip()
            cat = (m.get("category") or "—").strip()
            url = market_url(m)

            msg = (
                f"🟣 TOP PICK | Score={score:.1f}\n"
                f"{action}\n"
                f"🧠 Reason: {hint} | Spread={spr:.3f} (bid={bid:.3f}/ask={ask:.3f})\n"
                f"📊 Move={move_pct:+.2f}% | VolΔ={vol_delta:+.0f} | Liq={liq:.0f} | Vol24h={vol24:.0f}\n"
                f"📌 {title}\n"
                f"🏷️ {cat}\n"
                f"🔗 {url}"
            )
            tg_send(msg)
            set_cooldown(state, key)
            forced += 1
            sent += 1

    return sent, scanned, eligible, spread_out, errors


def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Bot will only log to console.")

    state = load_state()

    tg_send(
        "🤖 Bot ON (aggressive + SPREAD-SAFE)\n"
        f"Loop={LOOP_SECONDS}s | Pages={MAX_PAGES}x{PAGE_LIMIT} | Cooldown={COOLDOWN_MIN}min | ForceTopN={ALWAYS_SEND_TOPN}\n"
        f"MinLiq={MIN_LIQ:.0f} | MinVol24h={MIN_VOL24H:.0f} | MaxSpread={MAX_SPREAD:.2f} | MinBid={MIN_BID:.2f}\n"
        f"Cheap≤{CHEAP_MAX:.2f} | Expensive≥{EXPENSIVE_MIN:.2f} | ArbEdge≥{ARB_EDGE:.3f}\n"
        f"MoveAlert≥{MOVE_PCT_ALERT:.1f}% | VolΔAlert≥{VOL_DELTA_ALERT:.0f}"
    )

    while True:
        t0 = time.time()
        try:
            sent, scanned, eligible, spread_out, errs = scan_once(state)
            save_state(state)

            if SEND_DIAGNOSTIC:
                tg_send(
                    f"📊 Scan: scanned={scanned} | eligible(liq/vol)={eligible} | spread_filtered={spread_out} | alerts_sent={sent} | errors={errs}"
                )

            if errs:
                tg_send(f"⚠️ Warnings: {errs} errors (still running).")

        except Exception:
            tb = traceback.format_exc()
            print(tb)
            tg_send("⚠️ Bot error (still running). Check Railway logs.\n" + tb[:900])

        elapsed = time.time() - t0
        time.sleep(max(5, LOOP_SECONDS - int(elapsed)))


if __name__ == "__main__":
    main()
