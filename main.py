#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlencode

import requests

# =========================
# CONFIG (ajuste por ENV)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "600"))  # 10 min default
MIN_SCORE = float(os.getenv("MIN_SCORE", "18"))       # ↓ score mais baixo = +alertas
MAX_SPREAD_ABS = float(os.getenv("MAX_SPREAD_ABS", "0.045"))  # 4.5c
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.12"))   # 12%
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "2000"))     # gamma "liquidity"
MIN_VOL24 = float(os.getenv("MIN_VOL24", "200"))              # gamma "volume24hr" (varia por schema)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "7200")) # 2h por mercado+tipo
MAX_ALERTS_PER_SCAN = int(os.getenv("MAX_ALERTS_PER_SCAN", "8"))

# Arbitragem: custo de comprar YES+NO (asks) menor que 1 - buffer
ARB_BUFFER = float(os.getenv("ARB_BUFFER", "0.015"))  # 1.5% buffer p/ fees/slippage

# Momentum thresholds (mudança % desde último scan)
MOMO_MIN_MOVE_PCT = float(os.getenv("MOMO_MIN_MOVE_PCT", "0.9"))  # >=0.9%
MOMO_MAX_PRICE = float(os.getenv("MOMO_MAX_PRICE", "0.80"))       # não perseguir muito alto

# Endpoints
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

STATE_PATH = "/tmp/polybot_state.json"

UA = {"User-Agent": "poly-alert-bot/1.2"}

# =========================
# UTIL
# =========================
def now_ts() -> int:
    return int(time.time())

def clamp(x, a, b):
    return max(a, min(b, x))

def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def jget(d, keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur

# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. Can't send alerts.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        }
        r = requests.post(url, json=payload, timeout=15, headers=UA)
        if r.status_code >= 300:
            print("Telegram error:", r.status_code, r.text[:500])
            return False
        return True
    except Exception as e:
        print("Telegram exception:", repr(e))
        return False

# =========================
# POLYMARKET FETCHERS
# =========================
def fetch_gamma_markets(limit=200, max_pages=10):
    """
    Pull active markets from Gamma.
    Gamma schema can evolve; we try multiple filters and tolerate missing fields.
    """
    markets = []
    offset = 0
    for _ in range(max_pages):
        params = {
            "limit": limit,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        url = f"{GAMMA}/markets?{urlencode(params)}"
        r = requests.get(url, timeout=20, headers=UA)
        if r.status_code >= 300:
            # Fallback: try without closed filter
            params = {"limit": limit, "offset": offset, "active": "true"}
            url = f"{GAMMA}/markets?{urlencode(params)}"
            r = requests.get(url, timeout=20, headers=UA)
        r.raise_for_status()
        batch = r.json()
        if not isinstance(batch, list) or not batch:
            break
        markets.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            break
    return markets

def clob_price(token_id: str, side: str) -> float | None:
    """
    side: 'buy' or 'sell' (docs accept case-insensitive in examples)
    BUY returns lowest ask (what you pay). SELL returns highest bid (what you get).
    """
    url = f"{CLOB}/price?{urlencode({'token_id': token_id, 'side': side})}"
    r = requests.get(url, timeout=15, headers=UA)
    if r.status_code >= 300:
        return None
    data = r.json()
    return safe_float(data.get("price"))

def token_bid_ask(token_id: str):
    ask = clob_price(token_id, "buy")
    bid = clob_price(token_id, "sell")
    if ask is None or bid is None:
        return None, None, None, None
    spread = ask - bid
    spread_pct = (spread / ask) if ask > 0 else None
    return bid, ask, spread, spread_pct

# =========================
# STATE (dedup + momentum)
# =========================
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_mid": {}, "sent": {}}

def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        print("state save failed:", repr(e))

def sent_key(market_id: str, kind: str) -> str:
    return f"{market_id}:{kind}"

def can_send(state, market_id: str, kind: str) -> bool:
    k = sent_key(market_id, kind)
    last = state.get("sent", {}).get(k)
    if not last:
        return True
    return (now_ts() - int(last)) >= COOLDOWN_SECONDS

def mark_sent(state, market_id: str, kind: str):
    state.setdefault("sent", {})[sent_key(market_id, kind)] = now_ts()

# =========================
# SCORING LOGIC
# =========================
def liquidity_score(liq: float) -> float:
    # 0..10
    if liq is None:
        return 0.0
    return clamp(math.log10(max(liq, 1)) * 2.0, 0.0, 10.0)

def spread_penalty(spread_abs: float, spread_pct: float) -> float:
    # penalty 0..10 (maior spread => maior penalidade)
    if spread_abs is None or spread_pct is None:
        return 10.0
    # ABS pesa mais que pct (porque você quer sair)
    p1 = clamp((spread_abs / max(MAX_SPREAD_ABS, 1e-6)) * 6.0, 0.0, 10.0)
    p2 = clamp((spread_pct / max(MAX_SPREAD_PCT, 1e-6)) * 4.0, 0.0, 10.0)
    return clamp(p1 + p2 - 4.0, 0.0, 10.0)

def compute_momentum(mid_now: float, mid_prev: float | None):
    if mid_prev is None or mid_prev <= 0:
        return 0.0, 0.0
    pct = ((mid_now - mid_prev) / mid_prev) * 100.0
    return mid_now - mid_prev, pct

def score_arb(edge: float, liq: float, spr_pen: float) -> float:
    # edge em "dólar": (1 - (ask_yes+ask_no)) -> quanto "sobrou"
    base = clamp(edge * 1200.0, 0.0, 60.0)  # 0.02 edge => 24 pts
    return clamp(base + liquidity_score(liq) - spr_pen, 0.0, 100.0)

def score_momo(move_pct: float, liq: float, spr_pen: float) -> float:
    base = clamp(abs(move_pct) * 6.5, 0.0, 45.0)  # 2% => 13 pts
    return clamp(base + liquidity_score(liq) - spr_pen, 0.0, 100.0)

# =========================
# MESSAGE FORMAT
# =========================
def market_url(slug: str | None):
    if not slug:
        return "https://polymarket.com"
    return f"https://polymarket.com/market/{slug}"

def fmt_money(x):
    if x is None:
        return "n/a"
    return f"{x:,.0f}"

def build_alert(kind, title, url, details_lines):
    lines = []
    lines.append("🚨 BUY ALERT")
    lines.append(f"🎯 Type: {kind}")
    lines.append(f"🧩 {title}")
    lines.append("")
    lines.extend(details_lines)
    lines.append("")
    lines.append(url)
    return "\n".join(lines)

# =========================
# CORE LOOP
# =========================
def scan_once(state):
    alerts = []

    markets = fetch_gamma_markets()
    print(f"[scan] markets fetched: {len(markets)}")

    for m in markets:
        # --- Extract fields with tolerance to schema changes
        market_id = str(m.get("id") or m.get("conditionId") or m.get("condition_id") or "")
        if not market_id:
            continue

        active = m.get("active")
        closed = m.get("closed")
        if active is False or closed is True:
            continue

        title = m.get("question") or m.get("title") or m.get("name") or "Untitled market"
        slug = m.get("slug") or m.get("marketSlug") or m.get("market_slug")

        liq = safe_float(m.get("liquidity")) or safe_float(m.get("liquidityNum")) or safe_float(m.get("liquidityUSD")) or 0.0
        vol24 = safe_float(m.get("volume24hr")) or safe_float(m.get("volume24H")) or safe_float(m.get("volume24h")) or 0.0

        if liq < MIN_LIQUIDITY or vol24 < MIN_VOL24:
            continue

        # Token IDs (binary markets normally have 2)
        token_ids = m.get("clobTokenIds") or m.get("clobTokenIDs") or m.get("tokenIds") or m.get("token_ids")
        if not isinstance(token_ids, list) or len(token_ids) < 1:
            continue

        # Build token info
        token_info = []
        for tid in token_ids[:4]:  # cap safety
            tid = str(tid)
            bid, ask, spr_abs, spr_pct = token_bid_ask(tid)
            if bid is None or ask is None:
                continue
            if spr_abs is None or spr_pct is None:
                continue
            # Hard anti-spread gate
            if spr_abs > MAX_SPREAD_ABS or spr_pct > MAX_SPREAD_PCT:
                continue

            mid = (bid + ask) / 2.0
            prev_mid = state.get("last_mid", {}).get(tid)
            move_abs, move_pct = compute_momentum(mid, prev_mid)

            token_info.append({
                "token_id": tid,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spr_abs": spr_abs,
                "spr_pct": spr_pct,
                "move_pct": move_pct,
            })

        if len(token_info) == 0:
            continue

        # Update last_mid (for next scan)
        for t in token_info:
            state.setdefault("last_mid", {})[t["token_id"]] = t["mid"]

        # ----------------------
        # 1) ARBITRAGE (BUY BOTH)
        # ----------------------
        if len(token_info) >= 2:
            # pick two cheapest asks (usually YES/NO)
            sorted_by_ask = sorted(token_info, key=lambda x: x["ask"])
            t1, t2 = sorted_by_ask[0], sorted_by_ask[1]
            sum_asks = t1["ask"] + t2["ask"]
            edge = 1.0 - sum_asks  # if positive, theoretical "discount" before fees
            # Spread penalty: take worst of the two
            spr_pen = max(spread_penalty(t1["spr_abs"], t1["spr_pct"]),
                          spread_penalty(t2["spr_abs"], t2["spr_pct"]))

            if edge > ARB_BUFFER and can_send(state, market_id, "ARB"):
                sc = score_arb(edge=edge, liq=liq, spr_pen=spr_pen)
                if sc >= MIN_SCORE:
                    alerts.append({
                        "score": sc,
                        "text": build_alert(
                            kind="ARBITRAGE (BUY BOTH sides)",
                            title=title,
                            url=market_url(slug),
                            details_lines=[
                                f"🧠 Edge: {edge*100:.2f}% (BUY asks sum = {sum_asks:.3f})",
                                f"💧 Liquidity: ${fmt_money(liq)} | Vol24: ${fmt_money(vol24)}",
                                f"📉 Spread guard: max({MAX_SPREAD_ABS:.3f}$ / {MAX_SPREAD_PCT*100:.0f}%) ✅",
                                f"⭐ Score: {sc:.1f}",
                                "",
                                f"✅ ACTION: Consider BUY BOTH (two cheapest outcomes) with small size first.",
                                f"⚠️ Note: fees/slippage exist — buffer={ARB_BUFFER*100:.2f}% already applied."
                            ],
                        ),
                        "mark": ("ARB", market_id)
                    })

        # ----------------------
        # 2) MOMENTUM (BUY ONE)
        # ----------------------
        for t in token_info:
            # only if move is meaningful and price not too high
            if abs(t["move_pct"]) < MOMO_MIN_MOVE_PCT:
                continue
            if t["ask"] > MOMO_MAX_PRICE:
                continue

            spr_pen = spread_penalty(t["spr_abs"], t["spr_pct"])
            sc = score_momo(move_pct=t["move_pct"], liq=liq, spr_pen=spr_pen)

            # kind split to avoid spamming both tokens
            kind = f"MOMO:{t['token_id']}"
            if sc >= MIN_SCORE and can_send(state, market_id, kind):
                direction = "UP" if t["move_pct"] > 0 else "DOWN"
                alerts.append({
                    "score": sc,
                    "text": build_alert(
                        kind=f"MOMENTUM ({direction})",
                        title=title,
                        url=market_url(slug),
                        details_lines=[
                            f"📈 Move since last scan: {t['move_pct']:+.2f}%",
                            f"💰 Best prices: BUY(ask)={t['ask']:.3f} | SELL(bid)={t['bid']:.3f}",
                            f"🧷 Spread: {t['spr_abs']:.3f} ({t['spr_pct']*100:.1f}%) ✅",
                            f"💧 Liquidity: ${fmt_money(liq)} | Vol24: ${fmt_money(vol24)}",
                            f"⭐ Score: {sc:.1f}",
                            "",
                            f"✅ ACTION: Consider BUY this outcome token (small size first).",
                            f"🛡️ Exit focus: spread is within your max guardrails."
                        ],
                    ),
                    "mark": (kind, market_id)
                })

        # Early stop if too many alerts in one scan
        if len(alerts) >= MAX_ALERTS_PER_SCAN * 3:
            break

    # Sort & cap alerts per scan
    alerts.sort(key=lambda x: x["score"], reverse=True)
    alerts = alerts[:MAX_ALERTS_PER_SCAN]

    # Send
    sent_count = 0
    for a in alerts:
        ok = tg_send(a["text"])
        if ok:
            kind, mid = a["mark"]
            mark_sent(state, mid, kind)
            sent_count += 1
        time.sleep(0.6)

    print(f"[scan] alerts sent: {sent_count}")
    return sent_count

# =========================
# HEALTH SERVER (Railway)
# =========================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    print(f"[health] listening on 0.0.0.0:{port}")
    server.serve_forever()

# =========================
# MAIN
# =========================
def main():
    # start health
    threading.Thread(target=start_health_server, daemon=True).start()

    state = load_state()

    # Boot message once per deploy
    boot_msg = (
        "✅ BOT ON (arbitrage + momentum)\n"
        f"⏱️ Scan: every {SCAN_SECONDS//60} min\n"
        f"⭐ MIN_SCORE={MIN_SCORE}\n"
        f"🧷 MAX_SPREAD_ABS={MAX_SPREAD_ABS:.3f} | MAX_SPREAD_PCT={MAX_SPREAD_PCT*100:.0f}%\n"
        f"💧 MIN_LIQ=${MIN_LIQUIDITY:,.0f} | MIN_VOL24=${MIN_VOL24:,.0f}\n"
        "🎯 Only BUY alerts (as requested)"
    )
    tg_send(boot_msg)

    while True:
        try:
            scan_once(state)
            save_state(state)
        except Exception as e:
            print("❌ scan exception:", repr(e))
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    print("BOOT_OK: main.py running")
    main()
