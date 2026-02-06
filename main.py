#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import requests
from datetime import datetime, timezone

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

# bem agressivo
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))               # 1 min
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "60"))

# por padrão: SEM dedupe (0). Se quiser, coloque 30/60.
REPEAT_COOLDOWN_SEC = int(os.getenv("REPEAT_COOLDOWN_SEC", "0"))

# a cada X min manda um “resumo” caso esteja tudo zerado
DEBUG_EVERY_SEC = int(os.getenv("DEBUG_EVERY_SEC", "180"))        # 3 min

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

def clamp01(x):
    if x is None:
        return None
    return max(0.0, min(1.0, x))

def tg_api(method: str, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=timeout)
    return r.status_code, r.text, r.headers

def tg_send(text: str) -> bool:
    """
    Anti-rate-limit:
    - se 429, espera e tenta mais uma vez
    - loga erro sempre
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    status, body, headers = tg_api("sendMessage", payload)
    if status == 200:
        return True

    # rate limit
    if status == 429:
        # tenta extrair "retry_after"
        retry_after = 2
        try:
            # body costuma ter {"parameters":{"retry_after":X}}
            if "retry_after" in body:
                import re
                m = re.search(r"retry_after\":\s*(\d+)", body)
                if m:
                    retry_after = int(m.group(1))
        except Exception:
            pass
        print(f"⚠️ Telegram 429 rate limit. Sleeping {retry_after}s then retry.")
        time.sleep(retry_after)
        status2, body2, _ = tg_api("sendMessage", payload)
        if status2 == 200:
            return True
        print("❌ Telegram send failed after retry:", status2, body2[:400])
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

# =========================
# FETCH MARKETS (robusto)
# =========================
def fetch_markets(limit=900):
    attempts = [
        ("/markets", {"active":"true","closed":"false","limit":str(limit),"offset":"0","order":"volume24hr","ascending":"false"}),
        ("/markets", {"active":"true","closed":"false","limit":str(limit),"offset":"0"}),
        ("/markets", {"limit":str(limit),"offset":"0"}),
        ("/events",  {"active":"true","closed":"false","limit":str(min(limit, 200)),"offset":"0"}),
    ]

    last_err = None
    used = None
    for path, params in attempts:
        try:
            data = gamma_get(path, params=params)
            lst = extract_list(data)
            if lst:
                used = path
                if path == "/events":
                    markets = []
                    for ev in lst:
                        if isinstance(ev, dict) and isinstance(ev.get("markets"), list):
                            markets.extend(ev["markets"])
                    if markets:
                        return markets, None, used
                return lst, None, used
        except Exception as e:
            last_err = f"{path} failed: {repr(e)}"
            print(last_err)

    return [], last_err, used

# =========================
# PRICE PARSER
# =========================
def parse_yes_no(market: dict):
    yes = None
    no = None

    op = market.get("outcomePrices") or market.get("outcome_prices")
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no  = safe_float(op[1], None)

    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no  = safe_float(toks[1].get("price"), no)

    if yes is None:
        lp = market.get("lastPrice") or market.get("last_price")
        yes = safe_float(lp, None)
        if yes is not None and no is None:
            no = 1.0 - yes

    yes = clamp01(yes)
    no  = clamp01(no)
    if yes is None or no is None:
        return None, None
    return yes, no

def decide_buy(yes: float, no: float):
    # BUY-only: sempre escolhe um lado
    if yes < 0.5:
        return "BUY_YES"
    if yes > 0.5:
        return "BUY_NO"
    return "BUY_YES" if yes <= no else "BUY_NO"

def format_buy(market: dict, rec: str, yes: float, no: float):
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

# =========================
# OPTIONAL DEDUPE
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

def make_key(market: dict, rec: str, yes: float, no: float):
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    price = yes if rec == "BUY_YES" else no
    bucket = round(price, 3)
    return f"{mid}:{rec}:{bucket}"

# =========================
# MAIN
# =========================
def main():
    print("BOOT_OK: main.py running")
    tg_send(f"✅ Bot ON | BUY-only | sem score | poll={POLL_SECONDS}s | max/cycle={MAX_ALERTS_PER_CYCLE} | dedupe={REPEAT_COOLDOWN_SEC}s")

    last_debug = 0

    while True:
        markets, err, used = fetch_markets(limit=900)
        if not markets:
            print(f"[{now_utc()}] markets=0 err={err}")
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

            rec = decide_buy(yes, no)
            key = make_key(m, rec, yes, no)
            if should_send(key):
                candidates.append((m, rec, yes, no))

        sent = 0
        for m, rec, yes, no in candidates[:MAX_ALERTS_PER_CYCLE]:
            if tg_send(format_buy(m, rec, yes, no)):
                sent += 1

        print(f"[{now_utc()}] markets={len(markets)} parse_ok={parse_ok} candidates={len(candidates)} sent={sent} endpoint={used}")

        # Debug no Telegram (pra nunca ficar “mudo”)
        now = time.time()
        if sent == 0 and (now - last_debug) >= DEBUG_EVERY_SEC:
            tg_send(
                "🧩 DEBUG (sem alertas enviados)\n"
                f"markets={len(markets)} | parse_ok={parse_ok} | candidates={len(candidates)} | sent={sent}\n"
                f"endpoint={used} | dedupe={REPEAT_COOLDOWN_SEC}s | max/cycle={MAX_ALERTS_PER_CYCLE}\n"
                f"Hora: {now_utc()}"
            )
            last_debug = now

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
