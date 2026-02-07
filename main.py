#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import urllib.request
import urllib.parse
from datetime import datetime, timezone

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "50"))
REPEAT_COOLDOWN_SEC = int(os.getenv("REPEAT_COOLDOWN_SEC", "30"))

PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "250"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "6"))  # ~1500 mercados
DEBUG_EVERY_SEC = int(os.getenv("DEBUG_EVERY_SEC", "180"))

ARB_GAP_MIN = float(os.getenv("ARB_GAP_MIN", "0.007"))  # 0.7%
FEE_BUFFER  = float(os.getenv("FEE_BUFFER",  "0.003"))  # 0.3%
CHEAP_MAX_PRICE = float(os.getenv("CHEAP_MAX_PRICE", "0.10"))

SUM_ERR_SKIP = float(os.getenv("SUM_ERR_SKIP", "0.12"))
SUM_ERR_PEN_W = float(os.getenv("SUM_ERR_PEN_W", "60"))
SCORE_MIN = float(os.getenv("SCORE_MIN", "0"))

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

def http_get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)

def http_post_json(url: str, payload: dict, timeout: int = 20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        return e.code, body
    except Exception as e:
        return 0, repr(e)

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    status, body = http_post_json(url, payload, timeout=20)
    if status == 200:
        return True

    if status == 429 and "retry_after" in body:
        retry_after = 2
        try:
            data = json.loads(body)
            retry_after = int(data.get("parameters", {}).get("retry_after", 2))
        except Exception:
            pass
        print(f"⚠️ Telegram 429. Sleep {retry_after}s and retry.")
        time.sleep(retry_after)
        status2, body2 = http_post_json(url, payload, timeout=20)
        if status2 == 200:
            return True
        print("❌ Telegram send failed after retry:", status2, body2[:300])
        return False

    print("❌ Telegram send failed:", status, body[:300])
    return False

def gamma_get(path: str, params: dict, timeout: int = 25):
    qs = urllib.parse.urlencode(params)
    url = f"{GAMMA_URL}{path}?{qs}"
    return http_get_json(url, timeout=timeout)

def extract_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "markets", "results"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []

def fetch_markets_paged():
    all_markets = []
    used = "/markets"
    last_err = None

    for page in range(MAX_PAGES):
        offset = page * PAGE_LIMIT
        try:
            data = gamma_get("/markets", {
                "active": "true",
                "closed": "false",
                "limit": str(PAGE_LIMIT),
                "offset": str(offset),
                "order": "volume24hr",
                "ascending": "false",
            }, timeout=25)
            lst = extract_list(data)
            if not lst:
                break
            all_markets.extend(lst)
        except Exception as e:
            last_err = repr(e)
            break

    if not all_markets:
        try:
            used = "/events"
            data = gamma_get("/events", {
                "active": "true",
                "closed": "false",
                "limit": "200",
                "offset": "0",
            }, timeout=25)
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

def market_url(market: dict):
    slug = market.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = market.get("id") or market.get("conditionId") or market.get("condition_id")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

def get_liq_vol(market: dict):
    liq = safe_float(market.get("liquidity") or market.get("liquidityNum") or market.get("liquidity_num"), 0.0) or 0.0
    vol24 = safe_float(market.get("volume24hr") or market.get("volume24h") or market.get("volumeNum") or market.get("volume_num"), 0.0) or 0.0
    return liq, vol24

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
            return [p.strip().strip('"').strip("'") for p in s.split(",")]
    return None

def parse_yes_no(market: dict):
    yes = None
    no = None

    op_raw = market.get("outcomePrices") or market.get("outcome_prices")
    op = parse_outcome_prices(op_raw)
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no  = safe_float(op[1], None)

    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no  = safe_float(toks[1].get("price"), no)

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

def arb_gap(yes: float, no: float) -> float:
    return 1.0 - (yes + no)

def cheap_side(yes: float, no: float):
    if yes <= CHEAP_MAX_PRICE:
        return "CHEAP (BUY YES)", (CHEAP_MAX_PRICE - yes) / max(CHEAP_MAX_PRICE, 1e-9)
    if no <= CHEAP_MAX_PRICE:
        return "CHEAP (BUY NO)", (CHEAP_MAX_PRICE - no) / max(CHEAP_MAX_PRICE, 1e-9)
    return None, 0.0

def score_combo(net_gap: float, cheapness: float, liq: float, vol24: float, sum_err: float) -> float:
    gap_n = clamp(net_gap / 0.05, 0.0, 1.0)
    liq_n = clamp(math.log10(liq + 1.0) / 5.0, 0.0, 1.0)
    vol_n = clamp(math.log10(vol24 + 1.0) / 6.0, 0.0, 1.0)
    base = (60.0 * gap_n) + (22.0 * clamp(cheapness, 0.0, 1.0)) + (10.0 * liq_n) + (8.0 * vol_n)
    pen = min(SUM_ERR_PEN_W * sum_err, 45.0)
    return round(clamp(base - pen, 0.0, 100.0), 1)

def format_msg(market: dict, header: str, score: float, yes: float, no: float, liq: float, vol24: float, details: str):
    title = (market.get("question") or market.get("title") or "Market").strip()
    url = market_url(market)
    return (
        f"🚨 {header} | Score {score}\n"
        f"🧠 {title}\n"
        f"💰 YES {yes:.3f} | NO {no:.3f}\n"
        f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
        f"📝 {details}\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc()}"
    )

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

def boot():
    print("BOOT_OK: main.py running")
    tg_send(
        "✅ Bot ON | BUY-only | arbitrage+cheap+score (Docker-friendly)\n"
        f"gap_min={ARB_GAP_MIN*100:.2f}% | buffer={FEE_BUFFER*100:.2f}% | cheap≤{CHEAP_MAX_PRICE:.2f}\n"
        f"poll={POLL_SECONDS}s | pages={MAX_PAGES}x{PAGE_LIMIT} | max/cycle={MAX_ALERTS_PER_CYCLE} | score_min={SCORE_MIN}"
    )

def main():
    boot()
    last_debug = 0

    while True:
        markets, err, used = fetch_markets_paged()
        if not markets:
            tg_send(f"⚠️ Gamma 0 mercados.\nEndpoint: {used}\nErro: {err}\nHora: {now_utc()}")
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
            sum_err = abs((yes + no) - 1.0)

            gap = arb_gap(yes, no)
            net_gap = gap - FEE_BUFFER

            # dados estranhos: só aceita se arb muito forte
            if sum_err > SUM_ERR_SKIP and net_gap < (ARB_GAP_MIN * 2):
                continue

            # 1) Arbitragem real
            if net_gap >= ARB_GAP_MIN:
                _, cheapness = cheap_side(yes, no)
                score = score_combo(net_gap, cheapness, liq, vol24, sum_err)
                if score >= SCORE_MIN:
                    details = (
                        f"AÇÃO: BUY YES + BUY NO. YES+NO={yes+no:.3f} | "
                        f"Gap bruto={gap*100:.2f}% | Gap líquido≈{net_gap*100:.2f}% | spread_proxy={sum_err:.3f}"
                    )
                    key = make_key(m, "ARB", yes, no)
                    if should_send(key):
                        candidates.append((score, m, "ARBITRAGEM (BUY YES + BUY NO)", yes, no, liq, vol24, details))
                continue

            # 2) Cheap
            header, cheapness = cheap_side(yes, no)
            if header:
                score = score_combo(0.0, cheapness, liq, vol24, sum_err)
                if score >= SCORE_MIN:
                    details = f"AÇÃO: {header.replace('CHEAP ', '')}. cheap≤{CHEAP_MAX_PRICE:.2f} | spread_proxy={sum_err:.3f}"
                    key = make_key(m, "CHEAP", yes, no)
                    if should_send(key):
                        candidates.append((score, m, header, yes, no, liq, vol24, details))

        candidates.sort(key=lambda x: x[0], reverse=True)

        sent = 0
        for score, m, header, yes, no, liq, vol24, details in candidates[:MAX_ALERTS_PER_CYCLE]:
            if tg_send(format_msg(m, header, score, yes, no, liq, vol24, details)):
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
