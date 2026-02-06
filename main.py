#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import json
import requests
from datetime import datetime, timezone

# ======================================================
# CONFIG
# ======================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))  # 5 min default
SCORE_MIN = float(os.getenv("SCORE_MIN", "30"))       # min score default 30
REPEAT_COOLDOWN_MIN = int(os.getenv("REPEAT_COOLDOWN_MIN", "20"))  # repetir mesmo mercado depois de 20 min

# Polymarket Gamma API (public)
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com")

# Filtros para ficar agressivo (não travar demais)
MIN_LIQ = float(os.getenv("MIN_LIQ", "1500"))          # bem flexível
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "500"))  # flexível

# ======================================================
# HELPERS
# ======================================================
def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("Telegram error:", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
        return False

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except:
        return default

def sigmoid(x):
    # simples pra normalizar
    return 1.0 / (1.0 + math.exp(-x))

# ======================================================
# POLYMARKET FETCH
# ======================================================
def gamma_get(path, params=None, timeout=25):
    url = GAMMA_URL.rstrip("/") + path
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def fetch_markets(limit=200):
    """
    Puxa mercados ativos + com liquidez/volume.
    Gamma endpoints mudam às vezes; este padrão funciona bem.
    """
    # /markets costuma aceitar params: active=true, closed=false, limit, offset
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": "0",
        "order": "volume24hr",
        "ascending": "false",
    }
    data = gamma_get("/markets", params=params)

    # Gamma geralmente retorna lista direta
    if isinstance(data, list):
        return data
    # ou {"data":[...]}
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        return data["data"]
    return []

def parse_best_prices(market: dict):
    """
    Tenta obter preço atual YES/NO.
    Gamma geralmente traz outcomes/prices ou tokens.
    """
    yes = None
    no = None

    # Alguns mercados vêm com "outcomes" / "outcomePrices"
    # outcomePrices pode vir como lista de strings, ex ["0.43","0.57"]
    op = market.get("outcomePrices") or market.get("outcome_prices")
    outcomes = market.get("outcomes") or market.get("outcome")
    if op and isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no = safe_float(op[1], None)

    # Outros vêm com "tokens" que têm "price"
    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        # heuristic: tokens[0] = YES, tokens[1] = NO (comum)
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no = safe_float(toks[1].get("price"), no)

    # fallback: se tiver só lastPrice
    if yes is None:
        yes = safe_float(market.get("lastPrice") or market.get("last_price"), None)
        if yes is not None:
            no = 1.0 - yes if no is None else no

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
# SCORING + RECOMMENDATION (AGRESSIVO)
# ======================================================
def compute_score(market: dict):
    """
    Score 0-100 mais "esperto" mas ainda simples.
    A ideia: encontrar situações onde o preço mexeu e há liquidez/volume,
    e o spread não é absurdo.
    """
    liq = safe_float(market.get("liquidity"), 0.0)
    vol24 = safe_float(market.get("volume24hr") or market.get("volume24h"), 0.0)

    # movimentação recente (nem sempre existe)
    # Alguns retornam "priceChange24hr" etc
    price_chg_24 = safe_float(market.get("priceChange24hr") or market.get("price_change_24hr"), 0.0)
    # se vier em % já, ok; se vier em decimal, ainda serve como sinal
    abs_move = abs(price_chg_24)

    yes, no = parse_best_prices(market)
    if yes is None:
        return 0.0, None, {}

    # "spread proxy": se preços não somam ~1, pode ter ruído, ou dados incompletos
    spread_proxy = abs((yes + (no if no is not None else (1 - yes))) - 1.0)

    # normalizações (agressivas: aceitam valores menores)
    liq_n = clamp(math.log10(liq + 1) / 6.0, 0, 1)          # 1e6 -> ~1
    vol_n = clamp(math.log10(vol24 + 1) / 7.0, 0, 1)        # 1e7 -> ~1
    move_n = clamp(abs_move * 4.0, 0, 1)                    # amplifica

    # penaliza se spread_proxy muito alto (dados ruins / “armadilha”)
    spread_pen = clamp(spread_proxy * 8.0, 0, 0.6)

    # favorece preços "não extremos" (melhor pra entradas repetidas)
    mid_pref = 1.0 - abs(yes - 0.5) * 2.0  # 1 em 0.5, 0 em 0 ou 1
    mid_pref = clamp(mid_pref, 0, 1)

    raw = (
        45.0 * liq_n +
        35.0 * vol_n +
        25.0 * move_n +
        15.0 * mid_pref
    ) - (30.0 * spread_pen)

    score = clamp(raw, 0, 100)

    feats = {
        "liq": liq,
        "vol24": vol24,
        "abs_move": abs_move,
        "yes": yes,
        "no": no if no is not None else (1 - yes),
        "spread_proxy": spread_proxy,
    }
    return score, yes, feats

def decide_side(feats: dict):
    """
    Regra simples e clara:
    - se YES <= 0.45 e houve movimento/volume -> comprar YES (barato)
    - se YES >= 0.55 e houve movimento/volume -> comprar NO (YES caro)
    - no meio, escolhe lado pelo desvio (repetível)
    """
    yes = feats["yes"]
    no = feats["no"]
    abs_move = feats["abs_move"]
    vol24 = feats["vol24"]

    # “gatilho” de interesse (mais agressivo)
    interest = (abs_move >= 0.01) or (vol24 >= 5000)

    if yes <= 0.45 and interest:
        return "BUY_YES"
    if yes >= 0.55 and interest:
        return "BUY_NO"

    # neutro: pega o lado "mais barato" relativo ao 0.5
    if yes < 0.5:
        return "BUY_YES"
    return "BUY_NO"

def format_msg(market: dict, score: float, rec: str, feats: dict):
    title = (market.get("question") or market.get("title") or "Polymarket Market").strip()
    url = market_url(market)

    yes = feats["yes"]
    no = feats["no"]
    liq = feats["liq"]
    vol24 = feats["vol24"]
    mv = feats["abs_move"]

    if rec == "BUY_YES":
        action = "🟢 COMPRA: YES (a favor)"
        side_price = yes
        against_price = no
    else:
        action = "🔴 COMPRA: NO (contra)"
        side_price = no
        against_price = yes

    # motivo curto e direto
    reasons = []
    reasons.append(f"Preço alvo: {side_price:.3f} | Outro lado: {against_price:.3f}")
    reasons.append(f"Liquidez: {int(liq)} | Vol(24h): {int(vol24)}")
    reasons.append(f"Move(24h): {mv:.3f}")

    return (
        f"🚨 ALERTA (BUY) | Score: {score:.1f}\n"
        f"{action}\n"
        f"🧠 Mercado: {title}\n"
        f"📝 Motivo: " + " • ".join(reasons) + "\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc_str()}"
    )

# ======================================================
# DEDUPE (MAS PERMITE REPETIR)
# ======================================================
last_sent = {}  # key -> ts

def should_send(key: str):
    now = time.time()
    cooldown = REPEAT_COOLDOWN_MIN * 60
    ts = last_sent.get(key, 0)
    if now - ts >= cooldown:
        last_sent[key] = now
        return True
    return False

def make_key(market: dict, rec: str, feats: dict):
    # repete se preço mudar o suficiente
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    price = feats["yes"] if rec == "BUY_YES" else feats["no"]
    bucket = round(price, 3)  # sensível a pequenas mudanças
    return f"{mid}:{rec}:{bucket}"

# ======================================================
# MAIN LOOP
# ======================================================
def main():
    print("BOOT_OK: main.py running")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        # não dá pra avisar no Telegram, mas mantém health de container
    else:
        tg_send(f"✅ Bot ON | BUY-only | Score≥{SCORE_MIN} | poll={POLL_SECONDS}s | repeat={REPEAT_COOLDOWN_MIN}min")

    while True:
        try:
            markets = fetch_markets(limit=250)

            candidates = []
            for m in markets:
                # filtros mínimos bem flexíveis
                liq = safe_float(m.get("liquidity"), 0.0)
                vol24 = safe_float(m.get("volume24hr") or m.get("volume24h"), 0.0)
                if liq < MIN_LIQ or vol24 < MIN_VOLUME_24H:
                    continue

                score, yes, feats = compute_score(m)
                if yes is None:
                    continue
                if score < SCORE_MIN:
                    continue

                rec = decide_side(feats)

                # só recomendações de compra (BUY YES ou BUY NO)
                key = make_key(m, rec, feats)
                if should_send(key):
                    candidates.append((score, m, rec, feats))

            # manda os melhores primeiro (mas ainda pode mandar vários)
            candidates.sort(key=lambda x: x[0], reverse=True)

            # limite por ciclo (evita rajada)
            max_per_cycle = int(os.getenv("MAX_ALERTS_PER_CYCLE", "6"))
            sent = 0
            for score, m, rec, feats in candidates[:max_per_cycle]:
                msg = format_msg(m, score, rec, feats)
                ok = tg_send(msg)
                if ok:
                    sent += 1

            print(f"[{now_utc_str()}] markets={len(markets)} candidates={len(candidates)} sent={sent}")

        except Exception as e:
            print("Loop exception:", repr(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
