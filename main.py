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

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "120"))              # 2 min
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "40"))
REPEAT_COOLDOWN_SEC = int(os.getenv("REPEAT_COOLDOWN_SEC", "60")) # 1 min

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
    return r.status_code, r.text

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    status, body = tg_api("sendMessage", {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    })
    if status != 200:
        print("❌ Telegram sendMessage failed:", status, body[:500])
        return False
    return True

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
# FETCH (robusto)
# =========================
def fetch_markets(limit=700):
    attempts = [
        ("/markets", {"active":"true","closed":"false","limit":str(limit),"offset":"0","order":"volume24hr","ascending":"false"}),
        ("/markets", {"active":"true","closed":"false","limit":str(limit),"offset":"0"}),
        ("/markets", {"limit":str(limit),"offset":"0"}),
        ("/events",  {"active":"true","closed":"false","limit":str(min(limit, 200)),"offset":"0"}),
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
                        return markets, None, path
                return lst, None, path
        except Exception as e:
            last_err = f"{path} failed: {repr(e)}"
            print(last_err)

    return [], last_err, None

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
    # BUY-only, super agressivo
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
# DEDUPE leve (mas repete rápido)
# =========================
last_sent = {}

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
    bucket = round(price, 3)
    return f"{mid}:{rec}:{bucket}"

# =========================
# BOOT DIAGNOSTICS
# =========================
def boot_diagnostics():
    print("BOOT_OK: main.py running")

    # 1) mostra no log se env chegou
    print("ENV TELEGRAM_TOKEN len:", len(TELEGRAM_TOKEN))
    print("ENV TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID if TELEGRAM_CHAT_ID else "(empty)")
    print("GAMMA_URL:", GAMMA_URL)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Telegram env missing. Bot will NOT be able to message.")
        return False

    # 2) valida token
    st, body = tg_api("getMe", {})
    if st != 200:
        print("❌ getMe failed:", st, body[:500])
        # tenta avisar? não dá, pois token pode estar inválido
        return False

    # 3) força mensagem de teste (se isso não chegar, nada vai chegar)
    ok = tg_send(f"✅ BOOT TEST: Telegram OK. Bot iniciou em {now_utc()}.")
    if not ok:
        # manda detalhes do erro no log (já sai acima)
        return False

    # 4) valida fetch de mercados e manda contagem no Telegram
    markets, err, used = fetch_markets(limit=50)
    if not markets:
        tg_send(f"⚠️ BOOT TEST: Gamma retornou 0 mercados.\nErro: {err}\nHora: {now_utc()}")
        return True

    tg_send(f"✅ BOOT TEST: Gamma OK. mercados={len(markets)} (endpoint {used}). Hora: {now_utc()}")

    # 5) envia 3 BUYs “de teste” (pra provar que alertas chegam)
    sent = 0
    for m in markets[:10]:
        yes, no = parse_yes_no(m)
        if yes is None:
            continue
        rec = decide_buy(yes, no)
        tg_send("🧪 TEST BUY (amostra)\n" + format_buy(m, rec, yes, no))
        sent += 1
        if sent >= 3:
            break

    return True

# =========================
# MAIN LOOP
# =========================
def main():
    telegram_ok = boot_diagnostics()

    while True:
        try:
            markets, err, used = fetch_markets(limit=700)

            if not markets:
                # se Telegram ok, avisa a cada ~10 min
                print(f"[{now_utc()}] markets=0 err={err}")
                if telegram_ok:
                    tg_send(f"⚠️ API vazia/erro agora.\nEndpoint: {used}\nErro: {err}\nHora: {now_utc()}")
                    telegram_ok = True
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

            # manda bastante
            sent = 0
            for m, rec, yes, no in candidates[:MAX_ALERTS_PER_CYCLE]:
                if tg_send(format_buy(m, rec, yes, no)):
                    sent += 1

            print(f"[{now_utc()}] markets={len(markets)} candidates={len(candidates)} sent={sent} endpoint={used}")

        except Exception as e:
            print("Loop exception:", repr(e))
            if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
                tg_send(f"⚠️ Loop exception: {repr(e)}\nHora: {now_utc()}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
