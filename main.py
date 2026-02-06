#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import requests

# =========================
# CONFIG (constante + agressivo)
# =========================
# Scans mais frequentes
SCAN_EVERY_SECONDS = int(os.getenv("SCAN_EVERY_SECONDS", "90"))  # 1m30

# EDGE (entrada forte) — mais permissivo
EDGE_MIN_ABS = float(os.getenv("EDGE_MIN_ABS", "0.009"))         # 0.9c
EDGE_MIN_REL = float(os.getenv("EDGE_MIN_REL", "0.018"))         # 1.8%
LIQ_MIN = float(os.getenv("LIQ_MIN", "7000"))                    # liq mínima

# WATCH (quase-edge) — bem constante
WATCH_MIN_ABS = float(os.getenv("WATCH_MIN_ABS", "0.005"))       # 0.5c
WATCH_MIN_REL = float(os.getenv("WATCH_MIN_REL", "0.010"))       # 1.0%
WATCH_LIQ_MIN = float(os.getenv("WATCH_LIQ_MIN", "3000"))

# MOVE (capturar cedo)
MOVE_MIN_PCT = float(os.getenv("MOVE_MIN_PCT", "2.5"))           # 2.5%
VOL_MIN_DELTA = float(os.getenv("VOL_MIN_DELTA", "8000"))        # +8k

# PULSE (se não achar alertas, manda top candidatos)
PULSE_TOPK = int(os.getenv("PULSE_TOPK", "5"))
PULSE_MIN_LIQ = float(os.getenv("PULSE_MIN_LIQ", "2500"))
PULSE_COOLDOWN_SEC = int(os.getenv("PULSE_COOLDOWN_SEC", "300")) # 5 min

# Anti-spam
COOLDOWN_EDGE_SEC = int(os.getenv("COOLDOWN_EDGE_SEC", "1800"))  # 30 min
COOLDOWN_WATCH_SEC = int(os.getenv("COOLDOWN_WATCH_SEC", "600")) # 10 min
COOLDOWN_MOVE_SEC = int(os.getenv("COOLDOWN_MOVE_SEC", "600"))   # 10 min
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "12"))

# Diagnóstico quando vier 0 alertas
DIAG_COOLDOWN_SEC = int(os.getenv("DIAG_COOLDOWN_SEC", "1800"))  # 30 min

# Fonte de mercados (Gamma)
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

def compute_gap(yes, no):
    # gap > 0 => YES+NO < 1 (par “barato”)
    return 1.0 - (yes + no)


# =========================
# Parser YES/NO (mais robusto)
# =========================
def get_yes_no(m):
    # 1) Gamma comum: outcomePrices = [YES, NO]
    prices = m.get("outcomePrices") or m.get("outcome_prices")
    if isinstance(prices, list) and len(prices) >= 2:
        yes = clamp01(safe_float(prices[0], 0.0))
        no  = clamp01(safe_float(prices[1], 0.0))
        if yes > 0 and no > 0:
            return yes, no

    # 2) Alguns retornos: outcomePrices dict {"Yes":"0.xx","No":"0.xx"}
    if isinstance(prices, dict):
        yes = None
        no = None
        for k, v in prices.items():
            kk = str(k).lower()
            if "yes" in kk:
                yes = clamp01(safe_float(v, 0.0))
            if "no" in kk:
                no = clamp01(safe_float(v, 0.0))
        if yes and no:
            return yes, no

    # 3) Fallback: yesPrice/noPrice
    yes = safe_float(m.get("yesPrice"), 0.0)
    no  = safe_float(m.get("noPrice"), 0.0)
    if yes > 0 and no > 0:
        return clamp01(yes), clamp01(no)

    # 4) Alguns formatos vêm com "outcomes" + "prices"
    outcomes = m.get("outcomes")
    out_prices = m.get("outcomePrice") or m.get("outcome_price") or m.get("prices")
    # difícil generalizar sem o JSON, então só tentamos o básico:
    if isinstance(outcomes, list) and isinstance(out_prices, list) and len(outcomes) == len(out_prices):
        yes = None
        no = None
        for name, p in zip(outcomes, out_prices):
            nn = str(name).lower()
            if nn == "yes":
                yes = clamp01(safe_float(p, 0.0))
            if nn == "no":
                no = clamp01(safe_float(p, 0.0))
        if yes and no:
            return yes, no

    return None, None


# =========================
# Fetch markets
# =========================
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


# =========================
# Score (0–100)
# =========================
def norm01(x, lo, hi):
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))

def score_opportunity(gap, liq, move_pct, vol_delta):
    # Gap: bom em 0.5c, excelente em 3c
    s_gap = norm01(gap, 0.005, 0.030)
    # Liquidez: bom em 3k, excelente em 80k
    s_liq = norm01(liq, 3_000, 80_000)
    # Move: bom em 2%, excelente em 12%
    s_move = norm01(move_pct, 2.0, 12.0)
    # ΔVol: bom em 8k, excelente em 100k
    s_vd = norm01(vol_delta, 8_000, 100_000)
    total = (0.55 * s_gap) + (0.25 * s_liq) + (0.12 * s_move) + (0.08 * s_vd)
    return int(round(100 * total))


# =========================
# Recomendação clara
# =========================
def decide_action(yes_price):
    # Heurística simples: YES barato => A FAVOR (YES). YES caro => CONTRA (NO).
    if yes_price <= 0.50:
        return "A FAVOR (entrar YES)", "YES"
    return "CONTRA (entrar NO)", "NO"


# =========================
# State + cooldown
# =========================
last_seen = {}   # mid -> {yes,no,vol,ts}
last_sent = {}   # (mid, kind) -> ts
last_diag = 0

def should_send(mid, kind, cooldown_sec):
    ts = last_sent.get((mid, kind), 0)
    return (now_ts() - ts) >= cooldown_sec

def mark_sent(mid, kind):
    last_sent[(mid, kind)] = now_ts()

def maybe_diag(text):
    global last_diag
    if now_ts() - last_diag >= DIAG_COOLDOWN_SEC:
        tg_send(text)
        last_diag = now_ts()


# =========================
# Scan
# =========================
def scan_once():
    markets = fetch_markets()

    total = 0
    parsed = 0
    skipped_no_price = 0
    skipped_no_liq = 0

    cand_edge = 0
    cand_watch = 0
    cand_move = 0

    alerts = []
    pulse_candidates = []  # (score, text)

    for m in markets:
        total += 1
        mid = market_id(m)

        yes, no = get_yes_no(m)
        if yes is None or no is None:
            skipped_no_price += 1
            continue

        liq = get_liquidity(m)
        vol = get_volume(m)
        if liq <= 0:
            skipped_no_liq += 1
            continue

        parsed += 1

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
        action_txt, _side = decide_action(yes)
        sc = score_opportunity(gap, liq, move_pct, vol_delta)

        is_edge = (gap >= EDGE_MIN_ABS and rel >= EDGE_MIN_REL and liq >= LIQ_MIN)
        is_watch = (gap >= WATCH_MIN_ABS and rel >= WATCH_MIN_REL and liq >= WATCH_LIQ_MIN)
        is_move = (move_pct >= MOVE_MIN_PCT and vol_delta >= VOL_MIN_DELTA and liq >= WATCH_LIQ_MIN)

        reason = f"Score={sc}/100 | Gap={gap:+.3f} | YES={yes:.3f} | NO={no:.3f} | Liq={liq:.0f}"
        if prev:
            reason += f" | Move={move_pct:.1f}% | ΔVol={vol_delta:.0f}"

        if is_edge:
            cand_edge += 1
            if should_send(mid, "EDGE", COOLDOWN_EDGE_SEC):
                alerts.append((3, f"🚨 EDGE (entrada) — {sc}/100\n🧩 {q}\n🎯 AÇÃO: {action_txt}\n🧠 Motivo: {reason}\n🔗 {url}"))
                mark_sent(mid, "EDGE")

        elif is_watch:
            cand_watch += 1
            if should_send(mid, "WATCH", COOLDOWN_WATCH_SEC):
                alerts.append((2, f"👀 WATCH (quase-edge) — {sc}/100\n🧩 {q}\n🎯 AÇÃO: {action_txt}\n🧠 Motivo: {reason}\n🔗 {url}"))
                mark_sent(mid, "WATCH")

        elif is_move:
            cand_move += 1
            if should_send(mid, "MOVE", COOLDOWN_MOVE_SEC):
                alerts.append((1, f"⚡ MOVE + VOLUME — {sc}/100\n🧩 {q}\n🎯 AÇÃO: {action_txt}\n🧠 Motivo: {reason}\n🔗 {url}"))
                mark_sent(mid, "MOVE")

        # candidatos de PULSE (top do momento)
        if liq >= PULSE_MIN_LIQ and gap > 0:
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

    # ordenar alertas por prioridade/score
    alerts.sort(key=lambda x: x[0], reverse=True)
    msgs = [a[1] for a in alerts]

    # Se 0 alertas: tenta PULSE
    pulse_msg = None
    if not msgs:
        if should_send("GLOBAL", "PULSE", PULSE_COOLDOWN_SEC):
            pulse_candidates.sort(key=lambda x: x[0], reverse=True)
            top = [t[1] for t in pulse_candidates[:PULSE_TOPK]]
            if top:
                pulse_msg = "📍 PULSE (top agora — sem EDGE/WATCH/MOVE)\n" + "\n\n".join(top)
                mark_sent("GLOBAL", "PULSE")

    diag = (
        f"📊 Scan: total={total} | parse_ok={parsed} | EDGE_cand={cand_edge} | WATCH_cand={cand_watch} | MOVE_cand={cand_move}\n"
        f"⏭️ Pulados: sem_preço={skipped_no_price} | liq<=0={skipped_no_liq}\n"
        f"⚙️ Filtros: EDGE>=({EDGE_MIN_ABS:.3f},{EDGE_MIN_REL:.3f},liq{LIQ_MIN:.0f}) "
        f"WATCH>=({WATCH_MIN_ABS:.3f},{WATCH_MIN_REL:.3f},liq{WATCH_LIQ_MIN:.0f}) "
        f"PULSE(top{PULSE_TOPK},liq>={PULSE_MIN_LIQ:.0f})"
    )

    return msgs, pulse_msg, diag


# =========================
# Main (scan imediato + diagnóstico)
# =========================
def main():
    tg_send("🤖 Bot ON: Score 0–100 + A FAVOR/CONTRA | scan imediato + PULSE + diagnóstico.")

    # Scan IMEDIATO (pra você ver algo logo após o ON)
    try:
        msgs, pulse_msg, diag = scan_once()
        if msgs:
            for msg in msgs:
                tg_send(msg)
                time.sleep(1.1)
        elif pulse_msg:
            tg_send(pulse_msg)
        else:
            tg_send("ℹ️ Scan ok, mas sem alertas e sem pulse. Provável: parser/retorno sem preços ou filtros ainda altos.")
            tg_send(diag)
    except Exception as e:
        tg_send(f"⚠️ Erro no scan inicial: {e}")

    # Loop normal
    while True:
        try:
            msgs, pulse_msg, diag = scan_once()
            if msgs:
                for msg in msgs:
                    tg_send(msg)
                    time.sleep(1.1)
            elif pulse_msg:
                tg_send(pulse_msg)
            else:
                maybe_diag("ℹ️ Sem oportunidades agora.\n" + diag)
        except Exception as e:
            maybe_diag(f"⚠️ scan error: {e}")
        time.sleep(SCAN_EVERY_SECONDS)


if __name__ == "__main__":
    main()
