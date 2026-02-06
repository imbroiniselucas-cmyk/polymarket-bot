#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Polymarket Alert Bot — BUY-ONLY (NO WATCH) + Spread-Safe + Less Spam

Only sends alerts when the recommendation is a BUY:
✅ BUY YES
✅ BUY NO
✅ BUY BOTH (arb-ish)

Never sends:
❌ WATCH
❌ AVOID

Still protects you from bad exits:
- Requires bid/ask exist
- Requires spread <= MAX_SPREAD
- Requires bid >= MIN_BID

REQUIRED ENV (Railway Variables):
- TELEGRAM_TOKEN=xxxx
- TELEGRAM_CHAT_ID=yyyy

Defaults (low spam):
- LOOP_SECONDS=600
- MIN_LIQ=8000
- MIN_VOL24H=5000
- MAX_SPREAD=0.05
- MIN_BID=0.05
- SCORE_ALERT=65
- MOVE_PCT_ALERT=2.0
- VOL_DELTA_ALERT=8000
- COOLDOWN_MIN=35
- ALWAYS_SEND_TOPN=0        # important: no forced picks
- SEND_DIAGNOSTIC=1
"""

import os
import time
import json
import traceback
from typing import Any, Dict, List, Optional, Tuple

import requests


# ------------------------ ENV / Config ------------------------
TG_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

LOOP_SECONDS = int(os.getenv("LOOP_SECONDS", "600"))  # 10 min
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

MAX_PAGES = int(os.getenv("MAX_PAGES", "8"))
PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "200"))

MIN_LIQ = float(os.getenv("MIN_LIQ", "8000"))
MIN_VOL24H = float(os.getenv("MIN_VOL24H", "5000"))

MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))
MIN_BID = float(os.getenv("MIN_BID", "0.05"))

CHEAP_MAX = float(os.getenv("CHEAP_MAX", "0.12"))
ARB_EDGE = float(os.getenv("ARB_EDGE", "0.010"))

SCORE_ALERT = float(os.getenv("SCORE_ALERT", "65"))
MOVE_PCT_ALERT = float(os.getenv("MOVE_PCT_ALERT", "2.0"))
VOL_DELTA_ALERT = float(os.getenv("VOL_DELTA_ALERT", "8000"))

COOLDOWN_MIN = int(os.getenv("COOLDOWN_MIN", "35"))
ALWAYS_SEND_TOPN = int(os.getenv("ALWAYS_SEND_TOPN", "0"))  # default OFF for buy-only
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
    payload = {"chat_id": TG_CHAT_ID, "text": msg, "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code >= 300:
            print("Telegram error:", r.status_code, r.text[:400])
    except Exception as e:
        print("Telegram exception:", repr(e))


def market_url(m: Dict[str, Any]) -> str:
    slug = (m.get("slug") or "").strip()
    return PM_MARKET_BASE + slug if slug else "(no slug)"


def fetch_markets_page(offset: int, limit: int) -> List[Dict[str, Any]]:
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
        "order": "volume24hr",
        "ascending": "false",
    }
    r = requests.get(GAMMA_MARKETS_URL, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def parse_yes_no_prices(m: Dict[str, Any]) -> Optional[Tuple[float, float]]:
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


def spread_fields(m: Dict[str, Any]) -> Tuple[float, float, float]:
    bid = safe_float(m.get("bestBid"), 0.0)
    ask = safe_float(m.get("bestAsk"), 0.0)
    spr = safe_float(m.get("spread"), 0.0)
    if spr <= 0 and bid > 0 and ask > 0:
        spr = max(0.0, ask - bid)
    return bid, ask, spr


def pass_spread_safety(m: Dict[str, Any]) -> bool:
    bid, ask, spr = spread_fields(m)
    if bid <= 0 or ask <= 0:
        return False
    if bid < MIN_BID:
        return False
    if spr > MAX_SPREAD:
        return False
    return True


# ------------------------ BUY-ONLY Recommendation ------------------------
def buy_action(yes: float, no: float, arb_gap: float) -> Optional[Tuple[str, str]]:
    """
    Returns BUY action + hint, or None if it's not a BUY.
    """
    if arb_gap >= ARB_EDGE:
        return ("✅ BUY BOTH (ARB-ISH)", f"YES+NO={yes+no:.3f} gap={arb_gap:.3f}")
    if yes <= CHEAP_MAX:
        return ("✅ BUY YES (cheap)", f"YES={yes:.3f} ≤ {CHEAP_MAX:.2f}")
    if no <= CHEAP_MAX:
        return ("✅ BUY NO (cheap)", f"NO={no:.3f} ≤ {CHEAP_MAX:.2f}")
    return None  # WATCH/AVOID removed


def compute_score(yes: float, no: float, move_pct: float, vol_delta: float, spr: float, arb_gap: float) -> float:
    cheap_side = min(yes, no)
    cheapness = 0.0
    if cheap_side <= CHEAP_MAX:
        cheapness = (CHEAP_MAX - cheap_side) / max(CHEAP_MAX, 1e-9)

    arb_component = clamp(arb_gap / max(ARB_EDGE, 1e-9), 0.0, 2.0)
    move_component = clamp(abs(move_pct) / 6.0, 0.0, 1.5)
    vol_component = clamp(vol_delta / max(VOL_DELTA_ALERT, 1.0), 0.0, 1.5)
    spread_bonus = clamp((MAX_SPREAD - spr) / max(MAX_SPREAD, 1e-9), 0.0, 1.0)

    raw = 45.0 * cheapness + 30.0 * arb_component + 15.0 * move_component + 7.0 * vol_component + 3.0 * spread_bonus
    return clamp(raw, 0.0, 100.0)


def should_alert_buy(score: float, move_pct: float, vol_delta: float) -> bool:
    # Stricter trigger (less spam)
    if score >= SCORE_ALERT:
        return True
    if abs(move_pct) >= MOVE_PCT_ALERT and vol_delta >= (0.7 * VOL_DELTA_ALERT):
        return True
    if vol_delta >= VOL_DELTA_ALERT and abs(move_pct) >= 1.2:
        return True
    return False


def cooldown_ok(state: Dict[str, Any], key: str, score: float) -> bool:
    last = int(state.setdefault("cooldown", {}).get(key, 0) or 0)
    if score >= 85:
        return True
    return (now_ts() - last) >= (COOLDOWN_MIN * 60)


def set_cooldown(state: Dict[str, Any], key: str) -> None:
    state.setdefault("cooldown", {})[key] = now_ts()


# ------------------------ Scan Loop ------------------------
def scan_once(state: Dict[str, Any]) -> Tuple[int, int, int, int, int]:
    sent = 0
    scanned = 0
    eligible = 0
    spread_out = 0
    errors = 0

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

    candidates = []  # for optional forced buys only

    for m in markets:
        scanned += 1

        liq = safe_float(m.get("liquidityNum"), safe_float(m.get("liquidity"), 0.0))
        vol24 = safe_float(m.get("volume24hr"), 0.0)

        if liq < MIN_LIQ or vol24 < MIN_VOL24H:
            continue

        if not pass_spread_safety(m):
            spread_out += 1
            continue

        yn = parse_yes_no_prices(m)
        if not yn:
            continue
        yes, no = yn

        key = str(m.get("conditionId") or m.get("id") or m.get("slug") or "")
        if not key:
            continue

        bid, ask, spr = spread_fields(m)

        prev = seen.get(key, {})
        prev_yes = safe_float(prev.get("yes"), yes)
        prev_vol24 = safe_float(prev.get("vol24"), vol24)

        move_pct = ((yes - prev_yes) / prev_yes * 100.0) if prev_yes > 0 else 0.0
        vol_delta = vol24 - prev_vol24
        arb_gap = max(0.0, 1.0 - (yes + no))

        action = buy_action(yes, no, arb_gap)
        score = compute_score(yes, no, move_pct, vol_delta, spr, arb_gap)

        eligible += 1

        # only consider BUY candidates
        if action is not None:
            candidates.append((score, key, m, yes, no, liq, vol24, move_pct, vol_delta, arb_gap, bid, ask, spr, action))

            if should_alert_buy(score, move_pct, vol_delta) and cooldown_ok(state, key, score):
                action_line, hint = action
                title = (m.get("question") or "Market").strip()
                cat = (m.get("category") or "—").strip()
                url = market_url(m)

                msg = (
                    f"🟢 BUY ALERT | Score={score:.1f}\n"
                    f"{action_line}\n"
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

    # Optional: forced BUY only (kept OFF by default)
    if sent == 0 and ALWAYS_SEND_TOPN > 0 and candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        forced = 0
        for item in candidates:
            if forced >= ALWAYS_SEND_TOPN:
                break
            (score, key, m, yes, no, liq, vol24, move_pct, vol_delta, arb_gap, bid, ask, spr, action) = item
            if not cooldown_ok(state, key, score):
                continue
            action_line, hint = action
            title = (m.get("question") or "Market").strip()
            cat = (m.get("category") or "—").strip()
            url = market_url(m)

            msg = (
                f"🟣 FORCED BUY PICK | Score={score:.1f}\n"
                f"{action_line}\n"
                f"🧠 Reason: {hint} | Spread={spr:.3f} (bid={bid:.3f}/ask={ask:.3f})\n"
                f"📊 Move={move_pct:+.2f}% | VolΔ={vol_delta:+.0f} | Liq={liq:.0f} | Vol24h={vol24:.0f}\n"
                f"📌 {title}\n"
                f"🏷️ {cat}\n"
                f"🔗 {url}"
            )
            tg_send(msg)
            set_cooldown(state, key)
            sent += 1
            forced += 1

    return sent, scanned, eligible, spread_out, errors


def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Bot will only log to console.")

    state = load_state()

    tg_send(
        "🤖 Bot ON (BUY-ONLY + LESS SPAM)\n"
        f"Loop={LOOP_SECONDS}s | Cooldown={COOLDOWN_MIN}min | ForceBuyTopN={ALWAYS_SEND_TOPN}\n"
        f"MinLiq={MIN_LIQ:.0f} | MinVol24h={MIN_VOL24H:.0f} | MaxSpread={MAX_SPREAD:.2f} | MinBid={MIN_BID:.2f}\n"
        f"SCORE_ALERT≥{SCORE_ALERT:.0f} | MoveAlert≥{MOVE_PCT_ALERT:.1f}% | VolΔAlert≥{VOL_DELTA_ALERT:.0f}\n"
        f"BUY rules: CHEAP≤{CHEAP_MAX:.2f} or ARB gap≥{ARB_EDGE:.3f}"
    )

    while True:
        t0 = time.time()
        try:
            sent, scanned, eligible, spread_out, errs = scan_once(state)
            save_state(state)

            if SEND_DIAGNOSTIC:
                tg_send(
                    f"📊 Scan: scanned={scanned} | eligible={eligible} | spread_filtered={spread_out} | BUY_alerts_sent={sent} | errors={errs}"
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
