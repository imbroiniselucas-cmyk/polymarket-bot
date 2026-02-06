#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import requests
from datetime import datetime, timezone

# =========================
# CONFIG (agressividade)
# =========================
SCAN_EVERY_SECONDS = int(os.getenv("SCAN_EVERY_SECONDS", "300"))  # 5 min

# ALERTA "ENTRAR" (EDGE)
EDGE_MIN_ABS = float(os.getenv("EDGE_MIN_ABS", "0.015"))         # 1.5c de edge implícito
EDGE_MIN_REL = float(os.getenv("EDGE_MIN_REL", "0.030"))         # 3% relativo
LIQ_MIN = float(os.getenv("LIQ_MIN", "15000"))                   # liquidez mínima

# ALERTA "WATCH" (quase-edge, pra ficar constante)
WATCH_MIN_ABS = float(os.getenv("WATCH_MIN_ABS", "0.010"))       # 1.0c
WATCH_MIN_REL = float(os.getenv("WATCH_MIN_REL", "0.020"))       # 2%
WATCH_LIQ_MIN = float(os.getenv("WATCH_LIQ_MIN", "8000"))        # menor q EDGE

# MOVES (capturar cedo)
MOVE_MIN_PCT = float(os.getenv("MOVE_MIN_PCT", "4.0"))           # 4% de mudança no preço
VOL_MIN_DELTA = float(os.getenv("VOL_MIN_DELTA", "20000"))       # +20k de volume desde último scan

# Anti-spam
COOLDOWN_EDGE_SEC = int(os.getenv("COOLDOWN_EDGE_SEC", "2700"))  # 45 min por mercado (EDGE)
COOLDOWN_WATCH_SEC = int(os.getenv("COOLDOWN_WATCH_SEC", "1200"))# 20 min por mercado (WATCH)
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "12"))# limita spam por rodada

# Fonte de mercados (Gamma / Polymarket)
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com/markets")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# =========================
# Telegram
# =========================
def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=20).raise_for_status()
    except Exception as e:
        print("Telegram error:", e)


# =========================
# Polymarket fetch
# =========================
def fetch_markets():
    # Pegamos muitos e filtramos localmente
    params = {"limit": 200, "offset": 0, "active": "true"}
    allm = []
    while True:
        r = requests.get(GAMMA_URL, params=params, timeout=25)
        r.raise_for_status()
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk:
            break
        allm.extend(chunk)
        if len(chunk) < params["limit"]:
            break
        params["offset"] += params["limit"]
        if params["offset"] >= 2000:
            break
    return allm


def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except:
        return default


def now_ts():
    return int(time.time())


def pct(a, b):
    # % change from a -> b
    if a <= 0:
        return 0.0
    return ((b - a) / a) * 100.0


def clamp01(x):
    return max(0.0, min(1.0, x))


# =========================
# Market parsing helpers
# =========================
def market_url(m):
    # gamma costuma ter "slug"
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    # fallback
    mid = m.get("id") or m.get("marketId")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"


def get_best_yes_price(m):
    # Dependendo do objeto, vem em:
    # - m["outcomes"] / m["outcomePrices"]
    # - m["bestBid"]/["bestAsk"] etc
    # Aqui usamos a forma mais comum: "outcomePrices" (lista de strings)
    prices = m.get("outcomePrices") or m.get("outcome_prices")
    outcomes = m.get("outcomes") or m.get("outcomeNames") or m.get("outcome_names")

    if isinstance(prices, list) and len(prices) >= 2:
        # normalmente [YES, NO]
        yes = safe_float(prices[0], 0.0)
        no = safe_float(prices[1], 0.0)
        return clamp01(yes), clamp01(no)

    # fallback: tentar mapear via outcomes dict-like
    if isinstance(outcomes, list) and isinstance(prices, dict):
        # ex: prices {"Yes":"0.52","No":"0.48"}
        yes = None
        no = None
        for k, v in prices.items():
            kk = str(k).lower()
            if "yes" in kk:
                yes = safe_float(v, 0.0)
            if "no" in kk:
                no = safe_float(v, 0.0)
        if yes is not None and no is not None:
            return clamp01(yes), clamp01(no)

    # último fallback: alguns retornos têm "yesPrice"
    yes = safe_float(m.get("yesPrice"), 0.0)
    no = safe_float(m.get("noPrice"), 0.0)
    if yes > 0 and no > 0:
        return clamp01(yes), clamp01(no)

    return None, None


def get_liquidity(m):
    # gamma usa "liquidityNum" ou "liquidity"
    return safe_float(m.get("liquidityNum") or m.get("liquidity") or m.get("liquidity_num"), 0.0)


def get_volume(m):
    # gamma usa "volumeNum" ou "volume"
    return safe_float(m.get("volumeNum") or m.get("volume") or m.get("volume_num"), 0.0)


def title(m):
    return (m.get("question") or m.get("title") or m.get("name") or "Market").strip()


# =========================
# Logic
# =========================
# cache p/ moves e cooldown
last_seen = {}       # market_id -> dict(prices, vol, ts)
last_sent = {}       # (market_id, kind) -> ts

def market_id(m):
    return str(m.get("id") or m.get("marketId") or m.get("conditionId") or m.get("slug") or title(m))


def should_send(mid, kind, cooldown_sec):
    key = (mid, kind)
    ts = last_sent.get(key, 0)
    return (now_ts() - ts) >= cooldown_sec


def mark_sent(mid, kind):
    last_sent[(mid, kind)] = now_ts()


def compute_edge(yes, no):
    # Em mercado binário, um proxy simples:
    # se YES + NO < 1.0 => "gap" (subprecificado o par)
    # se YES + NO > 1.0 => "overround" (caro)
    s = yes + no
    gap = 1.0 - s
    # gap > 0 é “boa” (tem espaço). gap < 0 é “caro”.
    return gap


def decide_side(yes):
    # Heurística: se YES muito barato (<0.5), potencialmente valor em YES
    # se YES caro (>0.5), potencialmente valor em NO
    # (isso NÃO é “probabilidade real”, é só para recomendação clara)
    if yes <= 0.50:
        return "YES"
    return "NO"


def format_money(x):
    if x >= 1_000_000:
        return f"{x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x/1_000:.1f}K"
    return f"{x:.0f}"


def scan():
    markets = fetch_markets()
    alerts = []

    for m in markets:
        mid = market_id(m)
        yes, no = get_best_yes_price(m)
        if yes is None or no is None:
            continue

        liq = get_liquidity(m)
        vol = get_volume(m)
        if liq <= 0:
            continue

        edge = compute_edge(yes, no)
        edge_abs = edge
        edge_rel = (edge_abs / max(0.01, (yes + no)))  # relativo ao total do par

        # Move/volume delta
        prev = last_seen.get(mid)
        move_pct = 0.0
        vol_delta = 0.0
        if prev:
            move_pct = max(abs(pct(prev["yes"], yes)), abs(pct(prev["no"], no)))
            vol_delta = vol - prev["vol"]

        # atualizar cache
        last_seen[mid] = {"yes": yes, "no": no, "vol": vol, "ts": now_ts()}

        q = title(m)
        url = market_url(m)

        # Classificação
        is_edge = (edge_abs >= EDGE_MIN_ABS and edge_rel >= EDGE_MIN_REL and liq >= LIQ_MIN)
        is_watch = (edge_abs >= WATCH_MIN_ABS and edge_rel >= WATCH_MIN_REL and liq >= WATCH_LIQ_MIN)

        is_move = (move_pct >= MOVE_MIN_PCT and vol_delta >= VOL_MIN_DELTA and liq >= WATCH_LIQ_MIN)

        # Recomendação
        side = decide_side(yes)
        if side == "YES":
            rec_line = "🎯 AÇÃO: considerar entrada em **YES** (a favor do evento)"
        else:
            rec_line = "🎯 AÇÃO: considerar entrada em **NO** (contra o evento)"

        reason_bits = [
            f"Gap={edge_abs:+.3f}",
            f"YES={yes:.3f}",
            f"NO={no:.3f}",
            f"Liq={format_money(liq)}",
            f"Vol={format_money(vol)}"
        ]
        if prev:
            reason_bits.append(f"Move={move_pct:.1f}%")
            reason_bits.append(f"ΔVol={format_money(vol_delta)}")

        reason = " | ".join(reason_bits)

        # Decide alert type
        if is_edge and should_send(mid, "EDGE", COOLDOWN_EDGE_SEC):
            alerts.append((
                3,  # prioridade
                f"🚨 EDGE (entrada)\n🧩 {q}\n{rec_line}\n🧠 Motivo: {reason}\n🔗 {url}"
            ))
            mark_sent(mid, "EDGE")

        elif is_watch and should_send(mid, "WATCH", COOLDOWN_WATCH_SEC):
            alerts.append((
                2,
                f"👀 WATCH (quase-edge)\n🧩 {q}\n{rec_line}\n🧠 Motivo: {reason}\n🔗 {url}"
            ))
            mark_sent(mid, "WATCH")

        elif is_move and should_send(mid, "MOVE", COOLDOWN_WATCH_SEC):
            alerts.append((
                1,
                f"⚡ MOVE + VOLUME (cedo)\n🧩 {q}\n{rec_line}\n🧠 Motivo: {reason}\n🔗 {url}"
            ))
            mark_sent(mid, "MOVE")

        if len(alerts) >= MAX_ALERTS_PER_SCAN:
            break

    # ordenar por prioridade desc
    alerts.sort(key=lambda x: x[0], reverse=True)
    return [a[1] for a in alerts]


def main():
    tg_send("🤖 Bot ON: modo mais constante (EDGE + WATCH + MOVE).")
    while True:
        try:
            msgs = scan()
            for msg in msgs:
                tg_send(msg)
                time.sleep(1.2)
        except Exception as e:
            # erro silencioso (sem spam)
            print("scan error:", e)
        time.sleep(SCAN_EVERY_SECONDS)


if __name__ == "__main__":
    main()
