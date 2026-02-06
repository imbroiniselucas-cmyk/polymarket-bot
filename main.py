#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Opportunity Bot — MAX FLEX / AGGRESSIVE (5–10 min)

Key changes vs your old versions:
✅ MUCH lower default filters (liq + vol24h)
✅ Tiered filtering (lets small markets through, but scores them lower)
✅ Forces alerts every loop (TopN) so it won't stay silent
✅ Prints the REAL config it is using (so you can catch Railway ENV overriding)

Railway ENV (Variables) REQUIRED:
- TELEGRAM_TOKEN=xxxx
- TELEGRAM_CHAT_ID=yyyy

Optional ENV (defaults are aggressive/low):
- LOOP_SECONDS=300            # 300=5min, 600=10min
- MAX_EVENTS_PAGES=6          # scan more
- MIN_LIQ=500                 # default 500 (VERY flexible)
- MIN_VOL24H=100              # default 100
- CHEAP_MAX=0.15
- EXPENSIVE_MIN=0.90
- ARB_EDGE=0.008
- MOVE_PCT_ALERT=0.8
- VOL_DELTA_ALERT=1500
- COOLDOWN_MIN=8
- ALWAYS_SEND_TOPN=6          # guaranteed alerts per loop (unless everything is in cooldown)
- SEND_DIAGNOSTIC=1           # sends small “scan summary” each loop (optional)

IMPORTANT (Railway):
If you previously set MIN_LIQ=15000 or MIN_VOL24H=1000 in Railway Variables,
those ENV values will OVERRIDE the defaults. Remove them or lower them.
This code will show the config it's actually using in the first Telegram message.
"""

import os
import time
import json
import math
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests


# ------------------------ Config ------------------------
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "300"))
MAX_EVENTS_PAGES = int(os.getenv("MAX_EVENTS_PAGES", "6"))  # more coverage

# FLEXIBLE defaults (very low)
MIN_LIQ = float(os.getenv("MIN_LIQ", "500"))
MIN_VOL24H = float(os.getenv("MIN_VOL24H", "100"))

CHEAP_MAX = float(os.getenv("CHEAP_MAX", "0.15"))
EXPENSIVE_MIN = float(os.getenv("EXPENSIVE_MIN", "0.90"))

ARB_EDGE = float(os.getenv("ARB_EDGE", "0.008"))           # smaller arb gaps
MOVE_PCT_ALERT = float(os.getenv("MOVE_PCT_ALERT", "0.8")) # alert on smaller moves
VOL_DELTA_ALERT = float(os.getenv("VOL_DELTA_ALERT", "1500"))

COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "8"))         # faster repeats

ALWAYS_SEND_TOPN = int(os.getenv("ALWAYS_SEND_TOPN", "6")) # force alerts
SEND_DIAGNOSTIC = os.getenv("SEND_DIAGNOSTIC", "1").strip() not in ("0", "false", "False", "")

HTTP_TIMEOUT = 20
STATE_FILE = "state.json"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
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


def fetch_events_page(offset: int, limit: int = 100) -> List[Dict[str, Any]]:
    params = {
        "order": "id",
        "ascending": "false",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
    }
    r = requests.get(GAMMA_EVENTS_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def parse_outcome_prices(market: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")

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

    yes = mapping.get("yes", None)
    no = mapping.get("no", None)

    if yes is None or no is None:
        yes = safe_float(prices[0], 0.0)
        no = safe_float(prices[1], 0.0)

    yes = clamp(yes, 0.0, 1.0)
    no = clamp(no, 0.0, 1.0)
    return yes, no


def market_url(market: Dict[str, Any]) -> str:
    slug = (market.get("slug") or "").strip()
    if slug:
        return PM_MARKET_BASE + slug
    cid = (market.get("conditionId") or "").strip()
    return f"(no slug) conditionId={cid}"


def tier_weight(liq: float, vol24: float) -> float:
    """
    We allow small markets, but score them lower so spam is controlled.
    Returns multiplier ~0.55..1.15
    """
    # liquidity tier
    if liq >= 50000:
        l_w = 1.15
    elif liq >= 15000:
        l_w = 1.05
    elif liq >= 5000:
        l_w = 0.90
    elif liq >= 1500:
        l_w = 0.75
    else:
        l_w = 0.60

    # volume tier
    if vol24 >= 100000:
        v_w = 1.15
    elif vol24 >= 25000:
        v_w = 1.05
    elif vol24 >= 5000:
        v_w = 0.90
    elif vol24 >= 1000:
        v_w = 0.75
    else:
        v_w = 0.60

    return clamp((l_w + v_w) / 2.0, 0.55, 1.15)


def compute_score(
    yes: float,
    no: float,
    liq: float,
    vol24: float,
    move_pct: float,
    vol_delta: float,
    arb_gap: float,
) -> float:
    """
    Score 0..100
    Aggressive: rewards cheap prices + arb gaps + recent move + vol spike.
    Tier multiplier reduces junk from micro markets but still allows alerts.
    """
    cheap_side = min(yes, no)

    cheapness = 0.0
    if cheap_side <= CHEAP_MAX:
        cheapness = (CHEAP_MAX - cheap_side) / max(CHEAP_MAX, 1e-9)  # 0..1

    arb_component = clamp(arb_gap / max(ARB_EDGE, 1e-9), 0.0, 2.5)
    move_component = clamp(abs(move_pct) / 5.0, 0.0, 2.0)            # 5% -> 1
    vol_component = clamp(vol_delta / max(VOL_DELTA_ALERT, 1.0), 0.0, 2.5)

    base = (
        34.0 * cheapness +
        30.0 * arb_component +
        18.0 * move_component +
        18.0 * vol_component
    )

    mult = tier_weight(liq, vol24)
    return clamp(base * mult, 0.0, 100.0)


def recommendation_from_prices(yes: float, no: float, arb_gap: float) -> Tuple[str, str]:
    if arb_gap >= ARB_EDGE:
        return (
            "✅ ACTION: ARB-ISH → BUY BOTH (YES + NO) now",
            f"YES+NO={yes+no:.3f} (gap={arb_gap:.3f})",
        )

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
    # Easy triggers
    if score >= 45:
        return True
    if arb_gap >= ARB_EDGE:
        return True
    if (yes <= CHEAP_MAX) or (no <= CHEAP_MAX):
        return True
    if abs(move_pct) >= MOVE_PCT_ALERT:
        return True
    if vol_delta >= VOL_DELTA_ALERT:
        return True
    return False


def cooldown_ok(state: Dict[str, Any], cid: str, score: float) -> bool:
    last = int(state.setdefault("cooldown", {}).get(cid, 0) or 0)
    # let very strong opportunities break cooldown
    if score >= 80:
        return True
    return (now_ts() - last) >= (COOLDOWN_MIN * 60)


def set_cooldown(state: Dict[str, Any], cid: str) -> None:
    state.setdefault("cooldown", {})[cid] = now_ts()


# ------------------------ Scan Loop ------------------------
def scan_once(state: Dict[str, Any]) -> Tuple[int, int, int, int]:
    """
    Returns: sent, scanned_markets, kept_after_filters, errors
    """
    errors = 0
    scanned = 0
    kept = 0
    sent = 0

    seen = state.setdefault("seen", {})
    all_markets: List[Dict[str, Any]] = []

    for page in range(MAX_EVENTS_PAGES):
        offset = page * 100
        try:
            events = fetch_events_page(offset=offset, limit=100)
        except Exception as e:
            errors += 1
            print("Fetch events error:", repr(e))
            break

        for ev in events:
            mkts = ev.get("markets") or []
            if isinstance(mkts, list):
                for m in mkts:
                    if isinstance(m, dict):
                        all_markets.append(m)

    # dedupe by conditionId
    uniq: Dict[str, Dict[str, Any]] = {}
    for m in all_markets:
        cid = str(m.get("conditionId") or m.get("id") or m.get("slug") or "")
        if cid:
            uniq[cid] = m
    markets = list(uniq.values())

    # prioritize active
    def sort_key(m: Dict[str, Any]) -> float:
        return safe_float(m.get("volume24hr"), 0.0) + 0.08 * safe_float(m.get("liquidityNum"), 0.0)

    markets.sort(key=sort_key, reverse=True)

    candidates = []  # (score, ...)

    for m in markets:
        scanned += 1

        liq = safe_float(m.get("liquidityNum") or m.get("liquidity"), 0.0)
        vol24 = safe_float(m.get("volume24hr") or m.get("volume24Hour"), 0.0)

        # SUPER flexible hard-mins (still avoid totally dead)
        if liq < MIN_LIQ or vol24 < MIN_VOL24H:
            continue

        prices = parse_outcome_prices(m)
        if not prices:
            continue
        yes, no = prices

        cid = str(m.get("conditionId") or m.get("id") or m.get("slug") or "")
        if not cid:
            continue

        prev = seen.get(cid, {})
        prev_yes = safe_float(prev.get("yes"), yes)
        prev_vol24 = safe_float(prev.get("vol24"), vol24)

        move_pct = 0.0
        if prev_yes > 0:
            move_pct = ((yes - prev_yes) / prev_yes) * 100.0

        vol_delta = vol24 - prev_vol24
        arb_gap = max(0.0, 1.0 - (yes + no))

        score = compute_score(yes, no, liq, vol24, move_pct, vol_delta, arb_gap)
        candidates.append((score, cid, m, yes, no, liq, vol24, move_pct, vol_delta, arb_gap))

        kept += 1

        if should_alert(score, move_pct, vol_delta, arb_gap, yes, no) and cooldown_ok(state, cid, score):
            action, hint = recommendation_from_prices(yes, no, arb_gap)
            title = (m.get("question") or m.get("title") or "Market").strip()
            cat = (m.get("category") or "—").strip()
            url = market_url(m)

            msg = (
                f"🚨 ALERT | Score={score:.1f}\n"
                f"{action}\n"
                f"🧠 Reason: {hint} | Move={move_pct:+.2f}% | VolΔ={vol_delta:+.0f} | Liq={liq:.0f} | Vol24h={vol24:.0f}\n"
                f"📌 {title}\n"
                f"🏷️ {cat}\n"
                f"🔗 {url}"
            )
            tg_send(msg)
            set_cooldown(state, cid)
            sent += 1

        # update seen
        seen[cid] = {"yes": yes, "no": no, "vol24": vol24, "liq": liq, "ts": now_ts()}

    # Force-send TopN every loop if needed
    if ALWAYS_SEND_TOPN > 0:
        candidates.sort(key=lambda x: x[0], reverse=True)
        forced = 0
        for (score, cid, m, yes, no, liq, vol24, move_pct, vol_delta, arb_gap) in candidates:
            if forced >= ALWAYS_SEND_TOPN:
                break
            if not cooldown_ok(state, cid, score) and score < 85:
                continue

            action, hint = recommendation_from_prices(yes, no, arb_gap)
            title = (m.get("question") or m.get("title") or "Market").strip()
            cat = (m.get("category") or "—").strip()
            url = market_url(m)

            msg = (
                f"🟣 TOP PICK | Score={score:.1f}\n"
                f"{action}\n"
                f"🧠 Reason: {hint} | Move={move_pct:+.2f}% | VolΔ={vol_delta:+.0f} | Liq={liq:.0f} | Vol24h={vol24:.0f}\n"
                f"📌 {title}\n"
                f"🏷️ {cat}\n"
                f"🔗 {url}"
            )
            tg_send(msg)
            set_cooldown(state, cid)
            forced += 1
            sent += 1

    return sent, scanned, kept, errors


def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Bot will only log to console.")

    state = load_state()

    # This message is important: it shows EXACTLY what the bot is using (catches Railway ENV overrides).
    tg_send(
        "🤖 Bot ON (MAX FLEX / aggressive)\n"
        f"Loop={LOOP_SECONDS}s | Pages={MAX_EVENTS_PAGES} | Cooldown={COOLDOWN_MIN}min | ForceTopN={ALWAYS_SEND_TOPN}\n"
        f"MinLiq={MIN_LIQ:.0f} | MinVol24h={MIN_VOL24H:.0f}\n"
        f"Cheap≤{CHEAP_MAX:.2f} | Expensive≥{EXPENSIVE_MIN:.2f} | ArbEdge≥{ARB_EDGE:.3f}\n"
        f"MoveAlert≥{MOVE_PCT_ALERT:.1f}% | VolΔAlert≥{VOL_DELTA_ALERT:.0f}"
    )

    while True:
        t0 = time.time()
        try:
            sent, scanned, kept, errs = scan_once(state)
            save_state(state)

            if SEND_DIAGNOSTIC:
                tg_send(f"📊 Scan summary: scanned={scanned} | eligible={kept} | alerts_sent={sent} | errors={errs}")

            if errs:
                tg_send(f"⚠️ Warnings: {errs} fetch/parse errors (still running).")

        except Exception:
            tb = traceback.format_exc()
            print(tb)
            tg_send("⚠️ Bot error (still running). Check Railway logs.\n" + tb[:900])

        elapsed = time.time() - t0
        time.sleep(max(5, LOOP_SECONDS - int(elapsed)))


if __name__ == "__main__":
    main()
