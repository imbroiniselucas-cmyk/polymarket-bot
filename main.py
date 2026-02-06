#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import requests
from datetime import datetime, timezone

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "50"))
REPEAT_COOLDOWN_SEC = int(os.getenv("REPEAT_COOLDOWN_SEC", "30"))

PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "250"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "6"))  # ~1500

DEBUG_EVERY_SEC = int(os.getenv("DEBUG_EVERY_SEC", "180"))

# ---- Strategy knobs ----
# Arbitrage: requires (1 - (yes+no) - FEE_BUFFER) >= ARB_GAP_MIN
ARB_GAP_MIN = float(os.getenv("ARB_GAP_MIN", "0.007"))      # 0.7%
FEE_BUFFER  = float(os.getenv("FEE_BUFFER",  "0.003"))      # 0.3% (fees/slippage cushion)

# Cheap quotes thresholds
CHEAP_MAX_PRICE = float(os.getenv("CHEAP_MAX_PRICE", "0.10"))  # <= 0.10 is "cheap"
CHEAP_MID_BONUS = float(os.getenv("CHEAP_MID_BONUS", "0.02"))  # bonus if also near mid

# Spread trap / data sanity (proxy)
# sum_err = abs((yes+no) - 1.0). High => stale/odd data => penalize or skip
SUM_ERR_SKIP = float(os.getenv("SUM_ERR_SKIP", "0.12"))     # if > 0.12 skip unless arb is strong
SUM_ERR_PEN_W = float(os.getenv("SUM_ERR_PEN_W", "60"))     # penalty weight

# Optional score filter (0 = no filter)
SCORE_MIN = float(os.getenv("SCORE_MIN", "0"))              # set 30 if you want

# =========================
# HELPERS
# =========================
def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def clamp01(x):
    if x is None:
        return None
    return clamp(x, 0.0, 1.0)

def tg_api(method: str, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=timeout)
    return r.status_code, r.text

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    status, body = tg_api("sendMessage", payload)
    if status == 200:
        return True

    # Telegram rate limit
    if status == 429:
        retry_after = 2
        try:
            if "retry_after" in body:
                import re
                m = re.search(r"retry_after\":\s*(\d+)", body)
                if m:
                    retry_after = int(m.group(1))
        except Exception:
            pass

        print(f"⚠️ Telegram 429. Sleep {retry_after}s and retry.")
        time.sleep(retry_after)
        status2, body2 = tg_api("sendMessage", payload)
        if status2 == 200:
            return True
        print("❌ Telegram failed after retry:", status2, body2[:400])
        return False

    print("❌ Telegram sendMessage failed:", status, body[:400])
    return False

def gamma_get(path, params=None, timeout=25):
    url = GAMMA_URL + path
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def extract_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "markets", "results"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []

def market_url(market: dict):
    slug = market.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = market.get("id") or market.get("conditionId") or market.get("condition_id")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

def get_liq_vol(market: dict):
    liq = safe_float(
        market.get("liquidity")
        or market.get("liquidityNum")
        or market.get("liquidity_num"),
        0.0
    ) or 0.0
    vol24 = safe_float(
        market.get("volume24hr")
        or market.get("volume24h")
        or market.get("volumeNum")
        or market.get("volume_num"),
        0.0
    ) or 0.0
    return liq, vol24

# =========================
# FETCH MARKETS (PAGINATED)
# =========================
def fetch_markets_paged():
    all_markets = []
    used = "/markets"
    last_err = None

    for page in range(MAX_PAGES):
        offset = page * PAGE_LIMIT
        try:
            data = gamma_get("/markets", params={
                "active": "true",
                "closed": "false",
                "limit": str(PAGE_LIMIT),
                "offset": str(offset),
                "order": "volume24hr",
                "ascending": "false",
            })
            lst = extract_list(data)
            if not lst:
                break
            all_markets.extend(lst)
        except Exception as e:
            last_err = repr(e)
            break

    # fallback: /events
    if not all_markets:
        try:
            used = "/events"
            data = gamma_get("/events", params={
                "active": "true",
                "closed": "false",
                "limit": "200",
                "offset": "0",
            })
            evs = extract_list(data)
            mkts = []
            for ev in evs:
                if isinstance(ev, dict) and isinstance(ev.get("markets"), list):
                    mkts.extend(ev["markets"])
            if mkts:
                return mkts, None, used
        except Exception as e:
            last_err = repr(e)

    return all_markets, last_err, used

# =========================
# PARSE YES/NO (robust)
# =========================
def parse_outcome_prices(value):
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                return arr if isinstance(arr, list) else None
            except Exception:
                return None
        if "," in s:
            parts = [p.strip().strip('"').strip("'") for p in s.split(",")]
            return parts
    return None

def parse_yes_no(market: dict):
    yes = None
    no = None

    op_raw = market.get("outcomePrices") or market.get("outcome_prices")
    op = parse_outcome_prices(op_raw)
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no  = safe_float(op[1], None)

    # tokens fallback
    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no  = safe_float(toks[1].get("price"), no)

    # last price fallback
    if yes is None:
        lp = market.get("lastTradePrice") or market.get("lastPrice") or market.get("last_price")
        yes = safe_float(lp, None)
        if yes is not None and no is None:
            no = 1.0 - yes

    yes = clamp01(yes)
    no  = clamp01(no)
    if yes is None or no is None:
        return None, None
    return yes, no

# =========================
# SIGNALS: arbitrage / cheap / spread
# =========================
def arb_gap(yes: float, no: float) -> float:
    # Positive means YES+NO < 1 (discount)
    return 1.0 - (yes + no)

def mid_pref(yes: float) -> float:
    # 1 at 0.5, 0 at 0 or 1
    return clamp(1.0 - abs(yes - 0.5) * 2.0, 0.0, 1.0)

def cheap_side(yes: float, no: float):
    # Returns ("BUY_YES"/"BUY_NO", cheapness_value) or (None, 0)
    # cheapness_value bigger when price smaller
    if yes <= CHEAP_MAX_PRICE:
        return "BUY_YES", (CHEAP_MAX_PRICE - yes) / max(CHEAP_MAX_PRICE, 1e-9)
    if no <= CHEAP_MAX_PRICE:
        return "BUY_NO", (CHEAP_MAX_PRICE - no) / max(CHEAP_MAX_PRICE, 1e-9)
    return None, 0.0

def score_combo(yes: float, no: float, liq: float, vol24: float, net_gap: float, cheapness: float, sum_err: float) -> float:
    """
    Score 0-100:
    - Arbitrage net gap drives score most (up to 55)
    - Cheapness drives (up to 25)
    - Liquidity/Volume drive (up to 20)
    - Penalize spread proxy/sanity issues (sum_err)
    """
    # normalize net_gap: 0% -> 0, 5% -> 1
    gap_n = clamp(net_gap / 0.05, 0.0, 1.0)

    liq_n = clamp(math.log10(liq + 1.0) / 5.0, 0.0, 1.0)
    vol_n = clamp(math.log10(vol24 + 1.0) / 6.0, 0.0, 1.0)

    base = (55.0 * gap_n) + (25.0 * clamp(cheapness, 0.0, 1.0)) + (12.0 * liq_n) + (8.0 * vol_n)

    # extra small bonus if near mid (tradeable)
    base += CHEAP_MID_BONUS * 100.0 * mid_pref(yes)

    # penalty for “spread proxy” / odd data
    pen = min(SUM_ERR_PEN_W * sum_err, 45.0)

    return round(clamp(base - pen, 0.0, 100.0), 1)

# =========================
# OUTPUT FORMAT
# =========================
def format_alert(market: dict, kind: str, score: float, yes: float, no: float, liq: float, vol24: float, details: str):
    title = (market.get("question") or market.get("title") or "Market").strip()
    url = market_url(market)
    return (
        f"🚨 {kind} | Score {score}\n"
        f"🧠 {title}\n"
        f"💰 YES {yes:.3f} | NO {no:.3f}\n"
        f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
        f"📝 {details}\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc()}"
    )

# =========================
# DEDUPE
# =========================
last_sent = {}

def should_send(key: str):
    if REPEAT_COOLDOWN_SEC <= 0:
        return True
    now = time.time()
    ts = last_sent.get(key, 0)
    if now - ts >= REPEAT_COOLDOWN_SEC:
        last_sent[key] = now
        return True
    return False

def make_key(market: dict, kind: str, yes: float, no: float):
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    return f"{mid}:{kind}:{round(yes,3)}:{round(no,3)}"

# =========================
# MAIN
# =========================
def boot():
    print("BOOT_OK: main.py running")
    st, body = tg_api("getMe", {})
    if st != 200:
        print("❌ Telegram getMe failed:", st, body[:300])
    tg_send(
        "✅ Bot ON | BUY-only | arbitrage+spread+cheap+score\n"
        f"gap_min={ARB_GAP_MIN*100:.2f}% | buffer={FEE_BUFFER*100:.2f}% | cheap≤{CHEAP_MAX_PRICE:.2f}\n"
        f"poll={POLL_SECONDS}s | pages={MAX_PAGES}x{PAGE_LIMIT} | max/cycle={MAX_ALERTS_PER_CYCLE} | score_min={SCORE_MIN}"
    )

def main():
    boot()
    last_debug = 0

    while True:
        markets, err, used = fetch_markets_paged()
        if not markets:
            tg_send(f"⚠️ Gamma retornou 0 mercados.\nEndpoint: {used}\nErro: {err}\nHora: {now_utc()}")
            time.sleep(POLL_SECONDS)
            continue

        parse_ok = 0
        candidates = []

        for m in markets:
            yes, no = parse_yes_no(m)
            if yes is None:
                continue
            parse_ok += 1

            liq, vol24 = get_liq_vol(m)
            sum_err = abs((yes + no) - 1.0)  # spread proxy / sanity
            gap = arb_gap(yes, no)
            net_gap = gap - FEE_BUFFER

            # If data looks weird, skip unless arbitrage is really strong
            if sum_err > SUM_ERR_SKIP and net_gap < (ARB_GAP_MIN * 2):
                continue

            # 1) Arbitrage opportunity (buy both)
            if net_gap >= ARB_GAP_MIN:
                # cheapness can also contribute if one side is very low
                _, cheapness = cheap_side(yes, no)
                score = score_combo(yes, no, liq, vol24, net_gap, cheapness, sum_err)

                if score >= SCORE_MIN:
                    kind = "ARBITRAGEM (BUY YES + BUY NO)"
                    details = (
                        f"AÇÃO: comprar os dois lados. "
                        f"YES+NO={yes+no:.3f} | ArbGap bruto={gap*100:.2f}% | líquido≈{net_gap*100:.2f}% "
                        f"| spread_proxy={sum_err:.3f}"
                    )
                    key = make_key(m, "ARB", yes, no)
                    if should_send(key):
                        candidates.append((score, m, kind, yes, no, liq, vol24, details))
                continue  # arbitrage is top priority

            # 2) Cheap quote (single side)
            rec, cheapness = cheap_side(yes, no)
            if rec:
                # treat “cheap” as a smaller edge signal (net_gap=0)
                score = score_combo(yes, no, liq, vol24, 0.0, cheapness, sum_err)

                if score >= SCORE_MIN:
                    if rec == "BUY_YES":
                        kind = "CHEAP (BUY YES)"
                        details = f"AÇÃO: BUY YES (barato). yes={yes:.3f} ≤ {CHEAP_MAX_PRICE:.2f} | spread_proxy={sum_err:.3f}"
                    else:
                        kind = "CHEAP (BUY NO)"
                        details = f"AÇÃO: BUY NO (barato). no={no:.3f} ≤ {CHEAP_MAX_PRICE:.2f} | spread_proxy={sum_err:.3f}"

                    key = make_key(m, "CHEAP", yes, no)
                    if should_send(key):
                        candidates.append((score, m, kind, yes, no, liq, vol24, details))

        # send best first
        candidates.sort(key=lambda x: x[0], reverse=True)

        sent = 0
        for score, m, kind, yes, no, liq, vol24, details in candidates[:MAX_ALERTS_PER_CYCLE]:
            msg = format_alert(m, kind, score, yes, no, liq, vol24, details)
            if tg_send(msg):
                sent += 1

        print(f"[{now_utc()}] markets={len(markets)} parse_ok={parse_ok} candidates={len(candidates)} sent={sent} used={used}")

        now = time.time()
        if sent == 0 and (now - last_debug) >= DEBUG_EVERY_SEC:
            tg_send(
                "🧩 DEBUG (0 alertas enviados)\n"
                f"markets={len(markets)} | parse_ok={parse_ok} | candidates={len(candidates)} | sent={sent}\n"
                f"gap_min={ARB_GAP_MIN*100:.2f}% | buffer={FEE_BUFFER*100:.2f}% | cheap≤{CHEAP_MAX_PRICE:.2f}\n"
                f"sum_err_skip={SUM_ERR_SKIP} | score_min={SCORE_MIN} | endpoint={used}\n"
                f"Hora: {now_utc()}"
            )
            last_debug = now

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
