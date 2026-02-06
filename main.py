#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Opportunity Bot (Aggressive)
- Scans Polymarket Gamma API every 5/10 minutes
- Flags: (1) arbitrage-ish gaps, (2) "cheap" prices, (3) strong moves + volume spikes
- Sends CLEAR recommendations to Telegram (BUY YES / BUY NO / AVOID)
- No trading / no keys needed (read-only).

ENV needed (Railway Variables):
- TELEGRAM_TOKEN=xxxxxxxx
- TELEGRAM_CHAT_ID=123456789

Optional tuning:
- LOOP_SECONDS=300            # 300=5min, 600=10min
- MAX_EVENTS_PAGES=2          # each page = 100 events (newest first)
- MIN_LIQ=15000               # ignore illiquid
- MIN_VOL24H=10000            # ignore dead markets
- CHEAP_MAX=0.08              # <= this is considered "cheap"
- EXPENSIVE_MIN=0.92          # >= this is "expensive"
- MOVE_PCT_ALERT=3.0          # price move % since last scan to alert
- VOL_DELTA_ALERT=25000       # volume delta since last scan to alert
- ARB_EDGE=0.02               # if (YES + NO) < 1-ARB_EDGE => "arb-ish"
- COOLDOWN_MIN=25             # do not spam same market too often
- SEND_NOOP=1                 # send "no opportunities" summary each loop (1/0)
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

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "300"))  # 5min default
MAX_EVENTS_PAGES = int(os.getenv("MAX_EVENTS_PAGES", "2"))  # 2 pages * 100 events

MIN_LIQ = float(os.getenv("MIN_LIQ", "15000"))
MIN_VOL24H = float(os.getenv("MIN_VOL24H", "10000"))

CHEAP_MAX = float(os.getenv("CHEAP_MAX", "0.08"))
EXPENSIVE_MIN = float(os.getenv("EXPENSIVE_MIN", "0.92"))

MOVE_PCT_ALERT = float(os.getenv("MOVE_PCT_ALERT", "3.0"))
VOL_DELTA_ALERT = float(os.getenv("VOL_DELTA_ALERT", "25000"))

ARB_EDGE = float(os.getenv("ARB_EDGE", "0.02"))
COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "25"))

SEND_NOOP = os.getenv("SEND_NOOP", "1").strip() not in ("0", "false", "False", "")

HTTP_TIMEOUT = 20
STATE_FILE = "state.json"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
PM_MARKET_BASE = "https://polymarket.com/market/"  # slug appended


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
            print("Telegram error:", r.status_code, r.text[:300])
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
    if isinstance(data, list):
        return data
    return []


def parse_outcome_prices(market: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """
    Tries to return (yes_price, no_price) in [0..1].
    Gamma market often includes:
      - outcomes: '["Yes","No"]'
      - outcomePrices: '["0.43","0.57"]'
    Sometimes those are already arrays.
    """
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

    # map outcome->price
    mapping = {}
    for o, p in zip(outcomes, prices):
        mapping[str(o).strip().lower()] = safe_float(p, 0.0)

    # common names
    yes = mapping.get("yes", None)
    no = mapping.get("no", None)

    if yes is None or no is None:
        # fallback: first=YES second=NO (common)
        yes = safe_float(prices[0], 0.0)
        no = safe_float(prices[1], 0.0)

    # sanity clamp
    yes = clamp(yes, 0.0, 1.0)
    no = clamp(no, 0.0, 1.0)
    return yes, no


def market_url(market: Dict[str, Any]) -> str:
    slug = (market.get("slug") or "").strip()
    if slug:
        return PM_MARKET_BASE + slug
    # fallback: if no slug, give conditionId link hint
    cid = (market.get("conditionId") or "").strip()
    return f"{PM_MARKET_BASE}{slug}" if slug else f"(no slug) conditionId={cid}"


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
    Score 0..100 (bigger = better opportunity)
    """
    # cheapness: very low price gets rewarded
    cheapness = 0.0
    cheap_side = min(yes, no)
    if cheap_side <= CHEAP_MAX:
        cheapness = (CHEAP_MAX - cheap_side) / max(CHEAP_MAX, 1e-9)  # 0..1
    # move and volume
    move_component = clamp(abs(move_pct) / 10.0, 0.0, 1.0)  # 10% -> 1.0
    vol_component = clamp(vol_delta / max(VOL_DELTA_ALERT, 1.0), 0.0, 1.0)

    # liquidity/volume quality
    quality = clamp(math.log10(max(liq, 1.0)) / 6.0, 0.0, 1.0)  # ~1M liq -> ~1
    activity = clamp(math.log10(max(vol24, 1.0)) / 7.0, 0.0, 1.0)  # ~10M -> ~1

    # arb gap: if sum < 1, that's good
    arb_component = clamp(arb_gap / max(ARB_EDGE, 1e-9), 0.0, 1.5)  # allow >1

    raw = (
        35.0 * cheapness
        + 25.0 * arb_component
        + 15.0 * move_component
        + 10.0 * vol_component
        + 8.0 * quality
        + 7.0 * activity
    )
    return clamp(raw, 0.0, 100.0)


def recommendation_from_prices(yes: float, no: float, arb_gap: float) -> Tuple[str, str]:
    """
    Returns (action_line, rationale_hint)
    """
    # Arb-ish: buy both sides if sum < 1
    if arb_gap >= ARB_EDGE:
        return (
            "✅ ACTION: ARB-ISH → consider BUY BOTH (YES + NO) now",
            f"YES+NO={yes+no:.3f} (< 1 - {ARB_EDGE:.2f})",
        )

    # Cheap side
    if yes <= CHEAP_MAX:
        return (
            "✅ ACTION: BUY YES now (cheap)",
            f"YES={yes:.3f} ≤ {CHEAP_MAX:.2f}",
        )
    if no <= CHEAP_MAX:
        return (
            "✅ ACTION: BUY NO now (cheap)",
            f"NO={no:.3f} ≤ {CHEAP_MAX:.2f}",
        )

    # Very expensive side => contra / avoid
    if yes >= EXPENSIVE_MIN:
        return (
            "⚠️ ACTION: AVOID buying YES (looks expensive) → if you trade, consider NO instead",
            f"YES={yes:.3f} ≥ {EXPENSIVE_MIN:.2f}",
        )
    if no >= EXPENSIVE_MIN:
        return (
            "⚠️ ACTION: AVOID buying NO (looks expensive) → if you trade, consider YES instead",
            f"NO={no:.3f} ≥ {EXPENSIVE_MIN:.2f}",
        )

    return ("⏸ ACTION: WATCH (no clear edge)", "prices not extreme")


def should_alert(
    score: float,
    move_pct: float,
    vol_delta: float,
    cheap_flag: bool,
    arb_flag: bool,
) -> bool:
    if score >= 70:
        return True
    if arb_flag and score >= 45:
        return True
    if cheap_flag and score >= 40:
        return True
    if abs(move_pct) >= MOVE_PCT_ALERT and vol_delta >= (0.5 * VOL_DELTA_ALERT):
        return True
    if vol_delta >= VOL_DELTA_ALERT and abs(move_pct) >= 1.0:
        return True
    return False


# ------------------------ Main Scan ------------------------
def scan_once(state: Dict[str, Any]) -> Tuple[int, int, int]:
    """
    Returns: (opportunities_sent, markets_scanned, errors)
    """
    errors = 0
    markets_scanned = 0
    sent = 0

    # load cooldown maps
    cooldown = state.setdefault("cooldown", {})
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
                all_markets.extend([m for m in mkts if isinstance(m, dict)])

    # dedupe by conditionId if possible
    uniq = {}
    for m in all_markets:
        cid = (m.get("conditionId") or m.get("id") or m.get("slug") or str(id(m)))
        uniq[str(cid)] = m
    markets = list(uniq.values())

    # sort: higher activity first
    def sort_key(m: Dict[str, Any]) -> float:
        return safe_float(m.get("volume24hr"), 0.0) + 0.2 * safe_float(m.get("liquidityNum"), 0.0)

    markets.sort(key=sort_key, reverse=True)

    for m in markets:
        markets_scanned += 1

        liq = safe_float(m.get("liquidityNum") or m.get("liquidity"), 0.0)
        vol24 = safe_float(m.get("volume24hr") or m.get("volume24Hour"), 0.0)

        if liq < MIN_LIQ or vol24 < MIN_VOL24H:
            continue

        prices = parse_outcome_prices(m)
        if not prices:
            continue
        yes, no = prices

        # "arb gap": if yes+no < 1, gap = 1-(yes+no)
        sum_p = yes + no
        arb_gap = max(0.0, 1.0 - sum_p)

        cid = str(m.get("conditionId") or m.get("id") or m.get("slug") or "")
        if not cid:
            continue

        # previous
        prev = seen.get(cid, {})
        prev_yes = safe_float(prev.get("yes"), yes)
        prev_vol = safe_float(prev.get("vol24"), vol24)

        # movement (relative to previous scan)
        move_pct = 0.0
        if prev_yes > 0:
            move_pct = ((yes - prev_yes) / prev_yes) * 100.0

        vol_delta = vol24 - prev_vol

        # compute score
        score = compute_score(
            yes=yes,
            no=no,
            liq=liq,
            vol24=vol24,
            move_pct=move_pct,
            vol_delta=vol_delta,
            arb_gap=arb_gap,
        )

        cheap_flag = (yes <= CHEAP_MAX) or (no <= CHEAP_MAX)
        arb_flag = arb_gap >= ARB_EDGE

        # cooldown
        last_sent = int(cooldown.get(cid, 0) or 0)
        in_cooldown = (now_ts() - last_sent) < (COOLDOWN_MIN * 60)

        if should_alert(score, move_pct, vol_delta, cheap_flag, arb_flag) and (not in_cooldown or score >= 85):
            action, hint = recommendation_from_prices(yes, no, arb_gap)

            title = (m.get("question") or m.get("title") or "Market").strip()
            cat = (m.get("category") or "—").strip()
            url = market_url(m)

            msg = (
                f"🚨 OPPORTUNITY | Score={score:.1f}\n"
                f"{action}\n"
                f"🧠 Reason: {hint} | Move={move_pct:+.2f}% | VolΔ={vol_delta:+.0f} | Liq={liq:.0f} | Vol24h={vol24:.0f}\n"
                f"📌 {title}\n"
                f"🏷️ {cat}\n"
                f"🔗 {url}"
            )

            tg_send(msg)
            sent += 1
            cooldown[cid] = now_ts()

        # update seen
        seen[cid] = {"yes": yes, "no": no, "vol24": vol24, "liq": liq, "ts": now_ts()}

    # cleanup cooldown to avoid infinite growth
    # keep only last 10k
    if len(cooldown) > 10000:
        # drop oldest
        items = sorted(cooldown.items(), key=lambda kv: kv[1])
        cooldown = dict(items[-8000:])
        state["cooldown"] = cooldown

    return sent, markets_scanned, errors


def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Bot will only log to console.")

    state = load_state()

    tg_send(
        f"🤖 Bot ON (aggressive)\n"
        f"Loop={LOOP_SECONDS}s | Pages={MAX_EVENTS_PAGES} | MinLiq={MIN_LIQ:.0f} | MinVol24h={MIN_VOL24H:.0f}\n"
        f"Cheap≤{CHEAP_MAX:.2f} | ArbEdge≥{ARB_EDGE:.2f} | MoveAlert≥{MOVE_PCT_ALERT:.1f}% | VolΔAlert≥{VOL_DELTA_ALERT:.0f}"
    )

    while True:
        t0 = time.time()
        try:
            sent, scanned, errs = scan_once(state)
            save_state(state)

            if SEND_NOOP and sent == 0:
                tg_send(f"🔎 Scan OK: {scanned} markets checked | 0 opportunities (filters: liq≥{MIN_LIQ:.0f}, vol24h≥{MIN_VOL24H:.0f})")

            if errs:
                tg_send(f"⚠️ Scan warnings: {errs} errors (still running).")

        except Exception:
            tb = traceback.format_exc()
            print(tb)
            tg_send("⚠️ Bot error in scan loop (still running). Check Railway logs.\n" + tb[:900])

        # sleep remaining time
        elapsed = time.time() - t0
        sleep_s = max(5, LOOP_SECONDS - int(elapsed))
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
