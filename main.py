#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import requests
from datetime import datetime, timezone

# ======================================================
# CONFIG
# ======================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))          # 5 min
SCORE_MIN = float(os.getenv("SCORE_MIN", "20"))               # >= 20
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "10"))
REPEAT_COOLDOWN_MIN = int(os.getenv("REPEAT_COOLDOWN_MIN", "10"))

# Filtros mínimos (bem leves pra não ficar mudo)
MIN_LIQ = float(os.getenv("MIN_LIQ", "300"))
MIN_VOL24 = float(os.getenv("MIN_VOL24", "100"))

# ======================================================
# HELPERS
# ======================================================
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

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:250])
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
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
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("markets"), list):
            return payload["markets"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
    return []

# ======================================================
# FETCH MARKETS (robusto: tenta variações)
# ======================================================
def fetch_markets(limit=400):
    attempts = [
        ("/markets", {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": "0",
            "order": "volume24hr",
            "ascending": "false",
        }),
        # alguns ambientes aceitam "archived" / "resolved"
        ("/markets", {
            "active": "true",
            "archived": "false",
            "resolved": "false",
            "limit": str(limit),
            "offset": "0",
            "order": "volume24hr",
            "ascending": "false",
        }),
        # fallback sem order
        ("/markets", {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": "0",
        }),
        # último fallback: eventos (se markets estiver instável)
        ("/events", {
            "active": "true",
            "closed": "false",
            "limit": str(min(limit, 200)),
            "offset": "0",
        }),
    ]

    for path, params in attempts:
        try:
            data = gamma_get(path, params=params)
            lst = extract_list(data)
            if lst:
                # se veio /events, tenta extrair markets dentro
                if path == "/events":
                    markets = []
                    for ev in lst:
                        if isinstance(ev, dict) and isinstance(ev.get("markets"), list):
                            markets.extend(ev["markets"])
                    if markets:
                        return markets
                else:
                    return lst
        except Exception as e:
            print(f"fetch attempt failed {path}: {repr(e)}")
            continue

    return []

# ======================================================
# PRICE PARSER (YES/NO)
# ======================================================
def parse_yes_no(market: dict):
    yes = None
    no = None

    op = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no = safe_float(op[1], None)

    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no = safe_float(toks[1].get("price"), no)

    if yes is None:
        lp = market.get("lastPrice") or market.get("last_price")
        yes = safe_float(lp, None)
        if yes is not None and no is None:
            no = 1.0 - yes

    if yes is None or no is None:
        return None, None

    # normaliza
    yes = clamp(yes, 0.0, 1.0)
    no = clamp(no, 0.0, 1.0)
    return yes, no

def market_url(market: dict):
    slug = market.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = market.get("id") or market.get("conditionId") or market.get("condition_id")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

# ======================================================
# SCORE (não depende de priceChange)
# ======================================================
def compute_score(market: dict):
    liq = safe_float(market.get("liquidity"), 0.0) or 0.0
    vol24 = safe_float(market.get("volume24hr") or market.get("volume24h"), 0.0) or 0.0
    yes, no = parse_yes_no(market)
    if yes is None:
        return 0.0, None, {}

    # "mid preference": favorece preços próximos de 0.5 (mais tradeável)
    mid_pref = 1.0 - abs(yes - 0.5) * 2.0   # 1 no 0.5, 0 no 0/1
    mid_pref = clamp(mid_pref, 0, 1)

    # log-normaliza (bem tolerante)
    liq_n = clamp(math.log10(liq + 1) / 5.0, 0, 1)     # 1e5 ~ 1
    vol_n = clamp(math.log10(vol24 + 1) / 6.0, 0, 1)   # 1e6 ~ 1

    # penaliza dados estranhos (yes+no muito fora de 1)
    sum_err = abs((yes + no) - 1.0)
    err_pen = clamp(sum_err * 10.0, 0, 0.7)

    # Score 0-100
    score = (
        45.0 * liq_n +
        35.0 * vol_n +
        30.0 * mid_pref
    ) - (35.0 * err_pen)

    score = clamp(score, 0, 100)

    feats = {
        "liq": liq,
        "vol24": vol24,
        "yes": yes,
        "no": no,
        "sum_err": sum_err,
        "mid_pref": mid_pref,
    }
    return score, yes, feats

def decide_buy(feats: dict):
    """
    Só compra:
    - se YES <= 0.48 -> BUY YES
    - se YES >= 0.52 -> BUY NO
    - no meio, escolhe o lado "mais barato" (a favor de mean reversion)
    """
    yes = feats["yes"]
    if yes <= 0.48:
        return "BUY_YES"
    if yes >= 0.52:
        return "BUY_NO"
    return "BUY_YES" if yes < 0.5 else "BUY_NO"

def format_msg(market: dict, score: float, rec: str, feats: dict):
    title = (market.get("question") or market.get("title") or "Market").strip()
    url = market_url(market)

    yes = feats["yes"]
    no = feats["no"]
    liq = feats["liq"]
    vol24 = feats["vol24"]

    if rec == "BUY_YES":
        action = "🟢 COMPRA: YES (a favor)"
        px = yes
    else:
        action = "🔴 COMPRA: NO (contra)"
        px = no

    return (
        f"🚨 ALERTA (BUY) | Score {score:.1f}\n"
        f"{action}\n"
        f"🧠 {title}\n"
        f"💰 Preços: YES {yes:.3f} | NO {no:.3f} | alvo {px:.3f}\n"
        f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc()}"
    )

# ======================================================
# REPEAT CONTROL (permite repetir, mas evita flood)
# ======================================================
last_sent = {}  # key -> ts

def should_send(key: str):
    now = time.time()
    cd = REPEAT_COOLDOWN_MIN * 60
    ts = last_sent.get(key, 0)
    if now - ts >= cd:
        last_sent[key] = now
        return True
    return False

def make_key(market: dict, rec: str, feats: dict):
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    # bucket por preço pra repetir quando mexer
    price = feats["yes"] if rec == "BUY_YES" else feats["no"]
    bucket = round(price, 3)
    return f"{mid}:{rec}:{bucket}"

# ======================================================
# MAIN
# ======================================================
def main():
    print("BOOT_OK: main.py running")

    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        tg_send(f"✅ Bot ON | BUY-only | Score≥{SCORE_MIN} | poll={POLL_SECONDS}s | repeat={REPEAT_COOLDOWN_MIN}min")

    while True:
        try:
            markets = fetch_markets(limit=400)
            candidates = []

            for m in markets:
                liq = safe_float(m.get("liquidity"), 0.0) or 0.0
                vol24 = safe_float(m.get("volume24hr") or m.get("volume24h"), 0.0) or 0.0
                if liq < MIN_LIQ or vol24 < MIN_VOL24:
                    continue

                score, yes, feats = compute_score(m)
                if yes is None:
                    continue
                if score < SCORE_MIN:
                    continue

                rec = decide_buy(feats)  # BUY only
                key = make_key(m, rec, feats)

                if should_send(key):
                    candidates.append((score, m, rec, feats))

            candidates.sort(key=lambda x: x[0], reverse=True)

            sent = 0
            for score, m, rec, feats in candidates[:MAX_ALERTS_PER_CYCLE]:
                msg = format_msg(m, score, rec, feats)
                if tg_send(msg):
                    sent += 1

            print(f"[{now_utc()}] markets={len(markets)} candidates={len(candidates)} sent={sent}")

        except Exception as e:
            print("Loop exception:", repr(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
