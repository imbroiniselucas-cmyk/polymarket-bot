#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import requests
from datetime import datetime, timezone

# ======================================================
# CONFIG
# ======================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

# agressivo
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))  # 2 min
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "25"))
REPEAT_COOLDOWN_SEC = int(os.getenv("REPEAT_COOLDOWN_SEC", "180"))  # 3 min

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

def clamp01(x):
    if x is None:
        return None
    return max(0.0, min(1.0, x))

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
            print("Telegram error:", r.status_code, r.text[:300])
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

# ======================================================
# FETCH MARKETS (robusto, sem filtros)
# ======================================================
def fetch_markets(limit=500):
    attempts = [
        ("/markets", {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": "0",
            "order": "volume24hr",
            "ascending": "false",
        }),
        ("/markets", {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": "0",
        }),
        ("/markets", {
            "limit": str(limit),
            "offset": "0",
        }),
        ("/events", {
            "active": "true",
            "closed": "false",
            "limit": str(min(limit, 200)),
            "offset": "0",
        }),
    ]

    last_err = None
    for path, params in attempts:
        try:
            data = gamma_get(path, params=params)
            lst = extract_list(data)
            if lst:
                if path == "/events":
                    markets = []
                    for ev in lst:
                        if isinstance(ev, dict) and isinstance(ev.get("markets"), list):
                            markets.extend(ev["markets"])
                    if markets:
                        return markets, None
                return lst, None
        except Exception as e:
            last_err = f"{path} failed: {repr(e)}"
            print(last_err)

    return [], last_err

# ======================================================
# PRICE PARSER (YES/NO)
# ======================================================
def parse_yes_no(market: dict):
    yes = None
    no = None

    # outcomePrices: ["0.43","0.57"]
    op = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no = safe_float(op[1], None)

    # tokens: [{price:..},{price:..}]
    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no = safe_float(toks[1].get("price"), no)

    # lastPrice fallback
    if yes is None:
        lp = market.get("lastPrice") or market.get("last_price")
        yes = safe_float(lp, None)
        if yes is not None and no is None:
            no = 1.0 - yes

    yes = clamp01(yes)
    no = clamp01(no)

    if yes is None or no is None:
        return None, None
    return yes, no

# ======================================================
# BUY DECISION (sempre gera BUY)
# ======================================================
def decide_buy(yes: float, no: float):
    # regra super simples e agressiva:
    # - YES abaixo de 0.50 => BUY YES
    # - YES acima de 0.50 => BUY NO
    # - exatamente 0.50 => escolhe o mais barato (na prática tanto faz)
    if yes < 0.5:
        return "BUY_YES"
    if yes > 0.5:
        return "BUY_NO"
    return "BUY_YES" if yes <= no else "BUY_NO"

def format_msg(market: dict, rec: str, yes: float, no: float):
    title = (market.get("question") or market.get("title") or "Market").strip()
    liq = safe_float(market.get("liquidity"), 0.0) or 0.0
    vol24 = safe_float(market.get("volume24hr") or market.get("volume24h"), 0.0) or 0.0
    url = market_url(market)

    if rec == "BUY_YES":
        action = "🟢 COMPRA: YES (a favor)"
        alvo = yes
    else:
        action = "🔴 COMPRA: NO (contra)"
        alvo = no

    return (
        f"🚨 BUY ALERT\n"
        f"{action}\n"
        f"🧠 {title}\n"
        f"💰 YES {yes:.3f} | NO {no:.3f} | alvo {alvo:.3f}\n"
        f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc()}"
    )

# ======================================================
# DEDUPE (bem leve, mas permite repetir)
# ======================================================
last_sent = {}  # key -> ts

def should_send(key: str):
    now = time.time()
    ts = last_sent.get(key, 0)
    if now - ts >= REPEAT_COOLDOWN_SEC:
        last_sent[key] = now
        return True
    return False

def make_key(market: dict, rec: str, yes: float, no: float):
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    price = yes if rec == "BUY_YES" else no
    bucket = round(price, 3)  # repete quando mexer
    return f"{mid}:{rec}:{bucket}"

# ======================================================
# MAIN
# ======================================================
def main():
    print("BOOT_OK: main.py running")
    tg_send(f"✅ Bot ON | BUY-only | sem score/filtros | poll={POLL_SECONDS}s | max/cycle={MAX_ALERTS_PER_CYCLE}")

    last_warn = 0

    while True:
        try:
            markets, err = fetch_markets(limit=700)

            if not markets:
                # avisa (mas não spamma)
                now = time.time()
                if now - last_warn > 600:  # 10 min
                    msg = f"⚠️ Sem mercados retornados da Gamma API.\nErro: {err}\n🕒 {now_utc()}"
                    tg_send(msg)
                    last_warn = now
                print(f"[{now_utc()}] markets=0 err={err}")
                time.sleep(POLL_SECONDS)
                continue

            candidates = []
            for m in markets:
                yes, no = parse_yes_no(m)
                if yes is None:
                    continue

                rec = decide_buy(yes, no)
                key = make_key(m, rec, yes, no)

                if should_send(key):
                    candidates.append((m, rec, yes, no))

            # manda um monte (limitado)
            sent = 0
            for m, rec, yes, no in candidates[:MAX_ALERTS_PER_CYCLE]:
                if tg_send(format_msg(m, rec, yes, no)):
                    sent += 1

            print(f"[{now_utc()}] markets={len(markets)} candidates={len(candidates)} sent={sent}")

        except Exception as e:
            print("Loop exception:", repr(e))
            # avisa (mas não spamma)
            now = time.time()
            if now - last_warn > 600:
                tg_send(f"⚠️ Loop exception: {repr(e)}\n🕒 {now_utc()}")
                last_warn = now

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
