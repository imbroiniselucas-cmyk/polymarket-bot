#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import requests

# =========================
# CONFIG (constante + agressivo)
# =========================
SCAN_EVERY_SECONDS = int(os.getenv("SCAN_EVERY_SECONDS", "120"))  # 2 min

# EDGE (entrada forte)
EDGE_MIN_ABS = float(os.getenv("EDGE_MIN_ABS", "0.012"))         # 1.2c
EDGE_MIN_REL = float(os.getenv("EDGE_MIN_REL", "0.025"))         # 2.5%
LIQ_MIN = float(os.getenv("LIQ_MIN", "10000"))                   # liq mínima

# WATCH (quase-edge) — mais frequente
WATCH_MIN_ABS = float(os.getenv("WATCH_MIN_ABS", "0.007"))       # 0.7c
WATCH_MIN_REL = float(os.getenv("WATCH_MIN_REL", "0.012"))       # 1.2%
WATCH_LIQ_MIN = float(os.getenv("WATCH_LIQ_MIN", "5000"))

# MOVE (pegar cedo)
MOVE_MIN_PCT = float(os.getenv("MOVE_MIN_PCT", "3.0"))           # 3%
VOL_MIN_DELTA = float(os.getenv("VOL_MIN_DELTA", "12000"))       # +12k

# PULSE (se não achar alertas, manda top candidatos)
PULSE_TOPK = int(os.getenv("PULSE_TOPK", "3"))
PULSE_MIN_LIQ = float(os.getenv("PULSE_MIN_LIQ", "4000"))
PULSE_COOLDOWN_SEC = int(os.getenv("PULSE_COOLDOWN_SEC", "600")) # 10 min

# Anti-spam
COOLDOWN_EDGE_SEC = int(os.getenv("COOLDOWN_EDGE_SEC", "2400"))  # 40 min
COOLDOWN_WATCH_SEC = int(os.getenv("COOLDOWN_WATCH_SEC", "900")) # 15 min
COOLDOWN_MOVE_SEC = int(os.getenv("COOLDOWN_MOVE_SEC", "900"))   # 15 min
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "12"))

# Fonte de mercados
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com/markets")

# Telegram
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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        requests.post(url, json=payload, timeout=20).raise_for_status()
    except Exception as e:
        print("Telegram error:", e)


# =========================
# Helpers
# =========================
def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        return float(x)
    except:
        return default

def clamp01(x):
    return max(0.0, min(1.0, x))

def now_ts():
    return int(time.time())

def pct(a, b):
    if a <= 0:
        return 0.0
    return ((b - a) / a) * 100.0

def market_id(m):
    return str(m.get("id") or m.get("marketId") or m.get("conditionId") or m.get("slug") or (m.get("question") or "market"))

def title(m):
    return (m.get("question") or m.get("title") or m.get("name") or "Market").strip()

def market_url(m):
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = m.get("id") or m.get("marketId")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

def get_liquidity(m):
    return safe_float(m.get("liquidityNum") or m.get("liquidity") or m.get("liquidity_num"), 0.0)

def get_volume(m):
    return safe_float(m.get("volumeNum") or m.get("volume") or m.get("volume_num"), 0.0)

def get_yes_no(m):
    # Gamma comum: outcomePrices = [YES, NO]
    prices = m.get("outcomePrices") or m.get("outcome_prices")
    if isinstance(prices, list) and len(prices) >= 2:
        yes = clamp01(safe_float(prices[0], 0.0))
        no  = clamp01(safe_float(prices[1], 0.0))
        if yes > 0 and no > 0:
            return yes, no

    # Fallback (caso venha assim)
    yes = safe_float(m.get("yesPrice"), 0.0)
    no  = safe_float(m.get("noPrice"), 0.0)
    if yes > 0 and no > 0:
        return clamp01(yes), clamp01(no)

    return None, None

def fetch_markets():
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

def compute_gap(yes, no):
    # gap > 0 => "par barato" (YES+NO < 1)
    return 1.0 - (yes + no)


# =========================
# Score (0–100)
# =========================
def norm01(x, lo, hi):
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))

def score_opportunity(gap, liq, move_pct, vol_delta):
    """
    Score 0-100:
    - Gap domina (mais gap => mais score)
    - Liquidez melhora score
    - Move/ΔVol adiciona "urgência" (capturar cedo)
    """
    # Gap: consideramos "bom" a partir de ~0.5c, excelente em ~3.0c
    s_gap = norm01(gap, 0.005, 0.030)

    # Liquidez: razoável em 5k, excelente em 80k
    s_liq = norm01(liq, 5_000, 80_000)

    # Move: bom a partir de 2%, excelente em 12%
    s_move = norm01(move_pct, 2.0, 12.0)

    # Volume delta: bom em 10k, excelente em 100k
    s_vd = norm01(vol_delta, 10_000, 100_000)

    # Pesos
    total = (0.55 * s_gap) + (0.25 * s_liq) + (0.12 * s_move) + (0.08 * s_vd)
    return int(round(100 * total))


# =========================
# Recomendação clara (A FAVOR / CONTRA)
# =========================
def decide_action(yes_price):
    # Heurística simples e consistente:
    # YES barato -> A FAVOR (YES)
    # YES caro   -> CONTRA (NO)
    if yes_price <= 0.50:
        return "A FAVOR (entrar YES)", "YES"
    return "CONTRA (entrar NO)", "NO"


# =========================
# State
# =========================
last_seen = {}   # mid -> {yes,no,vol,ts}
last_sent = {}   # (mid, kind) -> ts

def should_send(mid, kind, cooldown_sec):
    ts = last_sent.get((mid, kind), 0)
    return (now_ts() - ts) >= cooldown_sec

def mark_sent(mid, kind):
    last_sent[(mid, kind)] = now_ts()


# =========================
# Scan
# =========================
def scan_once():
    markets = fetch_markets()

    alerts = []
    pulse_candidates = []  # (score, text) para PULSE

    for m in markets:
        mid = market_id(m)

        yes, no = get_yes_no(m)
        if yes is None or no is None:
            continue

        liq = get_liquidity(m)
        vol = get_volume(m)
        if liq <= 0:
            continue

        gap = compute_gap(yes, no)
        rel = gap / max(0.01, (yes + no))

        prev = last_seen.get(mid)
        move_pct = 0.0
        vol_delta = 0.0
        if prev:
            move_pct = max(abs(pct(prev["yes"], yes)), abs(pct(prev["no"], no)))
            vol_delta = vol - prev["vol"]

        last_seen[mid] = {"yes": yes, "no": no, "vol": vol, "ts": now_ts()}

        q = title(m)
        url = market_url(m)
        action_txt, side = decide_action(yes)

        sc = score_opportunity(gap, liq, move_pct, vol_delta)

        # regras
        is_edge = (gap >= EDGE_MIN_ABS and rel >= EDGE_MIN_REL and liq >= LIQ_MIN)
        is_watch = (gap >= WATCH_MIN_ABS and rel >= WATCH_MIN_REL and liq >= WATCH_LIQ_MIN)
        is_move = (move_pct >= MOVE_MIN_PCT and vol_delta >= VOL_MIN_DELTA and liq >= WATCH_LIQ_MIN)

        base_reason = f"Score={sc}/100 | Gap={gap:+.3f} | YES={yes:.3f} | NO={no:.3f} | Liq={liq:.0f}"
        if prev:
            base_reason += f" | Move={move_pct:.1f}% | ΔVol={vol_delta:.0f}"

        if is_edge and should_send(mid, "EDGE", COOLDOWN_EDGE_SEC):
            alerts.append((
                3,
                f"🚨 EDGE (entrada) — {sc}/100\n🧩 {q}\n🎯 AÇÃO: {action_txt}\n🧠 Motivo: {base_reason}\n🔗 {url}"
            ))
            mark_sent(mid, "EDGE")

        elif is_watch and should_send(mid, "WATCH", COOLDOWN_WATCH_SEC):
            alerts.append((
                2,
                f"👀 WATCH (quase-edge) — {sc}/100\n🧩 {q}\n🎯 AÇÃO: {action_txt}\n🧠 Motivo: {base_reason}\n🔗 {url}"
            ))
            mark_sent(mid, "WATCH")

        elif is_move and should_send(mid, "MOVE", COOLDOWN_MOVE_SEC):
            alerts.append((
                1,
                f"⚡ MOVE + VOLUME — {sc}/100\n🧩 {q}\n🎯 AÇÃO: {action_txt}\n🧠 Motivo: {base_reason}\n🔗 {url}"
            ))
            mark_sent(mid, "MOVE")

        # PULSE candidates (top 3 mesmo que não bata o mínimo)
        if liq >= PULSE_MIN_LIQ and gap > 0:
            # dica do que falta pra virar WATCH/EDGE
            falta_watch = max(0.0, WATCH_MIN_ABS - gap)
            falta_edge = max(0.0, EDGE_MIN_ABS - gap)
            need = []
            if falta_watch > 0:
                need.append(f"+{falta_watch:.3f} gap p/ WATCH")
            if falta_edge > 0:
                need.append(f"+{falta_edge:.3f} gap p/ EDGE")
            need_txt = " | ".join(need) if need else "já passou mínimos"
            pulse_candidates.append((
                sc,
                f"• {sc}/100 | {action_txt} | Gap={gap:+.3f} | Liq={liq:.0f}\n  {q}\n  ({need_txt})\n  {url}"
            ))

        if len(alerts) >= MAX_ALERTS_PER_SCAN:
            break

    # ordenar alertas por prioridade e score
    alerts.sort(key=lambda x: (x[0], int(x[1].split("—")[1].split("/")[0].strip())), reverse=True)
    msgs = [a[1] for a in alerts]

    # se não teve alertas, manda PULSE (top 3)
    pulse_msg = None
    if not msgs:
        if should_send("GLOBAL", "PULSE", PULSE_COOLDOWN_SEC):
            pulse_candidates.sort(key=lambda x: x[0], reverse=True)
            top = [t[1] for t in pulse_candidates[:PULSE_TOPK]]
            if top:
                pulse_msg = "📍 PULSE (top agora — sem EDGE/WATCH ainda)\n" + "\n\n".join(top)
                mark_sent("GLOBAL", "PULSE")

    return msgs, pulse_msg


def main():
    tg_send("🤖 Bot ON: Score 0–100 + recomendação A FAVOR/CONTRA (EDGE/WATCH/MOVE/PULSE).")
    while True:
        try:
            msgs, pulse_msg = scan_once()

            if msgs:
                for msg in msgs:
                    tg_send(msg)
                    time.sleep(1.1)
            elif pulse_msg:
                tg_send(pulse_msg)

        except Exception as e:
            # evita spam de erro
            print("scan error:", e)

        time.sleep(SCAN_EVERY_SECONDS)


if __name__ == "__main__":
    main()
