#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import requests
from datetime import datetime

# =========================
# ENV / CONFIG
# =========================

POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "https://polymarket.com/api/markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Loop
INTERVAL = int(os.getenv("INTERVAL_SEC", "900"))  # 15 min default
TOP_N = int(os.getenv("TOP_N", "5"))

# Filtros mínimos
MIN_LIQ = float(os.getenv("MIN_LIQ", "25000"))
MIN_VOL = float(os.getenv("MIN_VOL", "40000"))

# Arb clássico (YES+NO < 1 - cushion)
ARB_CUSHION = float(os.getenv("ARB_CUSHION", "0.015"))  # 1.5% "folga" (fees/slippage)

# Spread e sanity checks
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.12"))  # 12c
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.02"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.98"))

# Mispricing edge (fair-mid vs market)
BASE_MIN_EDGE = float(os.getenv("BASE_MIN_EDGE", "0.05"))  # 5% base
MAX_ENTRY_YES = float(os.getenv("MAX_ENTRY_YES", "0.68"))  # evita comprar YES caro

# Anti-spam e confirmação
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "3600"))  # 1h por mercado/tipo
REQUIRE_2_CYCLES = os.getenv("REQUIRE_2_CYCLES", "1") == "1"

# =========================
# SIMPLE STATE (memory)
# =========================

sent_cache = {}     # key -> timestamp
seen_once = set()   # keys vistos 1x (para confirmação em 2 ciclos)

# =========================
# TELEGRAM
# =========================

def send(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=12)
    except Exception:
        pass

# =========================
# HELPERS
# =========================

def now_ts():
    return time.time()

def cooldown_ok(key: str) -> bool:
    t = sent_cache.get(key, 0)
    return (now_ts() - t) > COOLDOWN_SEC

def clamp(x, a, b):
    return max(a, min(b, x))

def score_0_10(edge, liq, vol, spread, arb_gap=0.0):
    """
    Score heurístico 0-10:
    - Edge (ou arb_gap) pesa mais
    - Spread penaliza
    - liq/vol dão confiança
    """
    # normalizações
    e = max(edge, arb_gap)
    e_part = clamp(e / 0.12, 0, 1)  # 12% edge = "1.0"
    liq_part = clamp(math.log10(max(liq, 1)) / 6.0, 0, 1)  # ~1M liq -> bem alto
    vol_part = clamp(math.log10(max(vol, 1)) / 6.0, 0, 1)
    spr_pen = clamp(spread / MAX_SPREAD, 0, 1)

    raw = (0.55 * e_part) + (0.20 * liq_part) + (0.20 * vol_part) - (0.25 * spr_pen)
    return round(clamp(raw, 0, 1) * 10, 2)

def dynamic_min_edge(liq, vol):
    """
    Edge mínimo dinâmico:
    quanto maior liq/vol, menor edge mínimo aceito (mais “confiável”)
    """
    boost = 0.0
    if liq > 250000: boost += 0.01
    if liq > 750000: boost += 0.01
    if vol > 500000: boost += 0.01
    return max(0.03, BASE_MIN_EDGE - boost)  # nunca abaixo de 3%

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def fetch_markets():
    r = requests.get(POLY_ENDPOINT, timeout=20)
    r.raise_for_status()
    return r.json()

def market_url(m):
    slug = m.get("slug") or m.get("market_slug") or ""
    if slug:
        return f"https://polymarket.com/market/{slug}"
    # fallback
    mid = m.get("id", "")
    return f"https://polymarket.com/market?id={mid}"

def parse_market(m):
    """
    Tenta suportar pequenas variações no JSON.
    """
    title = m.get("title") or m.get("question") or "Untitled"
    mid = str(m.get("id") or m.get("market_id") or title)

    yes = safe_float(m.get("yesPrice") or m.get("yes_price"))
    no  = safe_float(m.get("noPrice")  or m.get("no_price"))

    vol = safe_float(m.get("volume") or m.get("volumeNum") or m.get("volume_num"), 0.0)
    liq = safe_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidity_num"), 0.0)

    if yes is None or no is None:
        return None

    # sanity
    if not (MIN_PRICE <= yes <= MAX_PRICE and MIN_PRICE <= no <= MAX_PRICE):
        return None

    # spread (se tiver bid/ask no endpoint, dá pra melhorar; aqui usa proxy simples)
    # proxy: spread = |(1-no) - yes| (quanto mais distante, pior a consistência)
    spread = abs((1.0 - no) - yes)

    return {
        "id": mid,
        "title": title.strip(),
        "yes": yes,
        "no": no,
        "vol": vol,
        "liq": liq,
        "spread": spread,
        "url": market_url(m),
    }

# =========================
# DETECTORS
# =========================

def detect_classic_arb(x):
    """
    Arb clássico: comprar YES e NO (ou estruturar) quando yes+no < 1 - cushion
    """
    s = x["yes"] + x["no"]
    # gap positivo é bom (quanto falta pra 1.0)
    arb_gap = (1.0 - s)
    if arb_gap <= ARB_CUSHION:
        return None
    # spread também importa
    if x["spread"] > MAX_SPREAD:
        return None
    # filtros
    if x["liq"] < MIN_LIQ or x["vol"] < MIN_VOL:
        return None

    key = f"{x['id']}|ARB"
    if REQUIRE_2_CYCLES:
        if key not in seen_once:
            seen_once.add(key)
            return None

    if not cooldown_ok(key):
        return None

    sc = score_0_10(edge=0.0, liq=x["liq"], vol=x["vol"], spread=x["spread"], arb_gap=arb_gap)

    return {
        "type": "ARB",
        "key": key,
        "title": x["title"],
        "url": x["url"],
        "yes": x["yes"],
        "no": x["no"],
        "liq": x["liq"],
        "vol": x["vol"],
        "spread": x["spread"],
        "arb_gap": arb_gap,
        "score": sc
    }

def detect_mispricing(x):
    """
    Mispricing: compara prob implícita (YES) vs fair (mid entre yes e 1-no).
    Fair heurístico: média entre yes e (1-no).
    """
    if x["liq"] < MIN_LIQ or x["vol"] < MIN_VOL:
        return None

    if x["spread"] > MAX_SPREAD:
        return None

    fair = (x["yes"] + (1.0 - x["no"])) / 2.0
    fair = clamp(fair, 0.0, 1.0)

    market_yes = x["yes"]
    edge_yes = fair - market_yes  # positivo => YES barato
    min_edge = dynamic_min_edge(x["liq"], x["vol"])

    # Só manda quando YES parece barato de verdade e não está caro
    if edge_yes < min_edge:
        return None
    if market_yes > MAX_ENTRY_YES:
        return None

    key = f"{x['id']}|YES"
    if REQUIRE_2_CYCLES:
        if key not in seen_once:
            seen_once.add(key)
            return None

    if not cooldown_ok(key):
        return None

    sc = score_0_10(edge=edge_yes, liq=x["liq"], vol=x["vol"], spread=x["spread"])

    return {
        "type": "YES",
        "key": key,
        "title": x["title"],
        "url": x["url"],
        "yes": x["yes"],
        "no": x["no"],
        "liq": x["liq"],
        "vol": x["vol"],
        "spread": x["spread"],
        "fair": fair,
        "edge": edge_yes,
        "score": sc
    }

# =========================
# MESSAGE FORMAT
# =========================

def fmt_money(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except:
        return str(n)

def fmt_pct(x):
    return f"{round(x*100, 2)}%"

def format_alert(a):
    if a["type"] == "ARB":
        return (
            "🚨 ARBITRAGEM (YES+NO)\n"
            "🎯 AÇÃO: ARB — comprar os dois lados (se conseguir executar)\n\n"
            f"📌 {a['title']}\n\n"
            f"💰 YES: {a['yes']:.3f} | NO: {a['no']:.3f}\n"
            f"🧮 YES+NO: {(a['yes']+a['no']):.3f}\n"
            f"✅ GAP ARB: {fmt_pct(a['arb_gap'])}\n"
            f"📏 Spread(proxy): {a['spread']:.3f}\n\n"
            f"⭐ Score: {a['score']}/10\n"
            f"💧 Liquidez: {fmt_money(a['liq'])}\n"
            f"📈 Volume: {fmt_money(a['vol'])}\n\n"
            f"🔗 {a['url']}"
        )

    # YES mispricing
    return (
        "🚨 MISPRICING (YES barato)\n"
        "🎯 AÇÃO: ENTRAR AGORA (YES)\n\n"
        f"📌 {a['title']}\n\n"
        f"💰 YES: {a['yes']:.3f} | NO: {a['no']:.3f}\n"
        f"🎯 Fair (heurística): {a['fair']:.3f}\n"
        f"⚖️ Edge estimado: +{fmt_pct(a['edge'])}\n"
        f"📏 Spread(proxy): {a['spread']:.3f}\n\n"
        f"⭐ Score: {a['score']}/10\n"
        f"💧 Liquidez: {fmt_money(a['liq'])}\n"
        f"📈 Volume: {fmt_money(a['vol'])}\n\n"
        f"🔗 {a['url']}"
    )

# =========================
# MAIN LOOP
# =========================

def main():
    send("⚡ POLY EDGE BOT ONLINE\nModo: ARB + MISPRICING (agressivo, filtrado)")

    while True:
        try:
            raw = fetch_markets()
            candidates = []

            for m in raw:
                x = parse_market(m)
                if not x:
                    continue

                # Detectores
                arb = detect_classic_arb(x)
                if arb:
                    candidates.append(arb)

                mis = detect_mispricing(x)
                if mis:
                    candidates.append(mis)

            # Rank por score e pega TOP_N
            candidates.sort(key=lambda z: z.get("score", 0), reverse=True)
            top = candidates[:TOP_N]

            if top:
                for a in top:
                    # registra cooldown
                    sent_cache[a["key"]] = now_ts()
                    send(format_alert(a))
            else:
                # sem spam no telegram, só print local
                print(f"[{datetime.utcnow().isoformat()}] sem oportunidades válidas")

        except Exception as e:
            print("Erro:", repr(e))

        time.sleep(INTERVAL)

if __name__ == "__main__":
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID ausente/invalid")

    main()
