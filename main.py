#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot — “aggressive mode” (back like 2 days ago) but keeps methodology:
- Still scores markets (volume + move + gap + liquidity)
- Still anti-spam (cooldown per market + hourly cap)
- MUCH less silence (lower thresholds + “top movers” fallback ping)

✅ What changed to stop 12h silence:
1) Lower default thresholds (more aggressive)
2) Stronger pagination + safer params (fetch more markets reliably)
3) Fallback: if no alerts for X minutes, send Top 3 “highest score now” markets (so you ALWAYS get something)
4) Heartbeat every 15 min with “scanned / binary / candidates / alerts”

ENV (Railway):
- TELEGRAM_TOKEN=...
- CHAT_ID=...

Optional tuning (defaults are aggressive):
- POLY_BASE=https://gamma-api.polymarket.com
- POLL_SECONDS=45
- SCORE_MIN=4.5
- GAP_CENTS_MIN=0.5
- COOLDOWN_MINUTES=10
- HEARTBEAT_MINUTES=15
- MAX_ALERTS_PER_HOUR=35
- NO_ALERT_FALLBACK_MINUTES=45
- FALLBACK_TOPN=3
- PAGES=6
- PAGE_SIZE=200
"""

import os
import time
import json
import math
import requests
from datetime import datetime, timezone

import telebot

# -----------------------------
# Config
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()

POLY_BASE = os.getenv("POLY_BASE", "https://gamma-api.polymarket.com").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "45"))
SCORE_MIN = float(os.getenv("SCORE_MIN", "4.5"))
GAP_CENTS_MIN = float(os.getenv("GAP_CENTS_MIN", "0.5"))  # 0.5¢ -> more alerts
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "10"))
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "15"))
MAX_ALERTS_PER_HOUR = int(os.getenv("MAX_ALERTS_PER_HOUR", "35"))

NO_ALERT_FALLBACK_MINUTES = int(os.getenv("NO_ALERT_FALLBACK_MINUTES", "45"))
FALLBACK_TOPN = int(os.getenv("FALLBACK_TOPN", "3"))

PAGES = int(os.getenv("PAGES", "6"))
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "200"))

STATE_FILE = os.getenv("STATE_FILE", "state.json")

REQUEST_TIMEOUT = 20
UA = "Mozilla/5.0 (compatible; PolymarketAlertBot/2.0)"

if not TELEGRAM_TOKEN or not CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_TOKEN or CHAT_ID env vars.")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# -----------------------------
# Helpers
# -----------------------------
def now_ts() -> int:
    return int(time.time())

def utc_iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")

def safe_float(x, default=0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "":
            return default
        return float(s)
    except Exception:
        return default

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def send(msg: str):
    # extra safety: never crash on Telegram hiccups
    try:
        bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)
    except Exception:
        pass

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "markets": {},               # market_id -> snapshot
            "alerts": [],                # timestamps (for hourly cap)
            "last_heartbeat": 0,
            "last_any_alert": 0,
            "last_fallback": 0
        }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
            st.setdefault("markets", {})
            st.setdefault("alerts", [])
            st.setdefault("last_heartbeat", 0)
            st.setdefault("last_any_alert", 0)
            st.setdefault("last_fallback", 0)
            return st
    except Exception:
        return {
            "markets": {},
            "alerts": [],
            "last_heartbeat": 0,
            "last_any_alert": 0,
            "last_fallback": 0
        }

def save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)

def cleanup_state(state: dict):
    """Trim old alert timestamps + stale market snapshots so bot doesn’t ‘freeze’."""
    t = now_ts()

    # keep last 3 hours of alert timestamps
    state["alerts"] = [a for a in state.get("alerts", []) if (t - a) <= 3 * 3600]

    # remove market snapshots unseen for 72h
    markets = state.get("markets", {})
    stale = []
    for mid, snap in markets.items():
        last_seen = int(snap.get("last_seen", 0))
        if last_seen and (t - last_seen) > 72 * 3600:
            stale.append(mid)
    for mid in stale:
        markets.pop(mid, None)
    state["markets"] = markets

def can_send_alert(state: dict) -> bool:
    t = now_ts()
    one_hour_ago = t - 3600
    last_hour = [a for a in state.get("alerts", []) if a >= one_hour_ago]
    return len(last_hour) < MAX_ALERTS_PER_HOUR

def record_alert(state: dict):
    t = now_ts()
    state.setdefault("alerts", []).append(t)
    state["last_any_alert"] = t

# -----------------------------
# Fetch markets (Gamma API)
# -----------------------------
def fetch_markets(limit_pages: int, page_size: int) -> list:
    """
    Safer fetch: avoids relying on sort keys that might change.
    Pages through more results to include non-crypto categories too.
    """
    out = []
    offset = 0
    headers = {"User-Agent": UA}

    for _ in range(limit_pages):
        url = f"{POLY_BASE}/markets"
        params = {
            "limit": page_size,
            "offset": offset,
            "active": "true",
            "closed": "false",
        }
        r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()

        chunk = data if isinstance(data, list) else (data.get("data", []) if isinstance(data, dict) else [])
        if not chunk:
            break

        out.extend(chunk)
        offset += page_size

    return out

def extract_binary_prices(m: dict):
    """
    Returns (yes_price, no_price) for binary Yes/No markets.
    Gamma commonly has:
      outcomes: ["Yes","No"]
      outcomePrices: ["0.43","0.57"]
    """
    outcomes = m.get("outcomes") or []
    prices = m.get("outcomePrices") or []

    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return (None, None)
    if len(outcomes) != len(prices) or len(outcomes) < 2:
        return (None, None)

    idx_yes = idx_no = None
    for i, o in enumerate(outcomes):
        s = str(o).strip().lower()
        if s == "yes":
            idx_yes = i
        elif s == "no":
            idx_no = i

    if idx_yes is None or idx_no is None:
        return (None, None)

    yes = safe_float(prices[idx_yes], default=None)
    no = safe_float(prices[idx_no], default=None)
    if yes is None or no is None:
        return (None, None)

    return (clamp(yes, 0.0, 1.0), clamp(no, 0.0, 1.0))

def compute_gap_cents(yes: float, no: float) -> float:
    # In ideal binary, yes + no ~ 1.00
    gap = max(0.0, 1.0 - (yes + no))
    return gap * 100.0

def score_market(vol_delta: float, price_move_abs: float, gap_cents: float, liquidity: float) -> float:
    """
    Aggressive scoring: small moves/gaps still count.
    """
    vol_component = math.log10(1.0 + max(0.0, vol_delta))      # 0..+
    move_component = (price_move_abs * 100.0)                  # percentage points
    gap_component = gap_cents                                   # cents
    liq_component = math.log10(1.0 + max(0.0, liquidity))      # 0..+

    # slightly higher weight on move/gap so it fires more often
    return (2.0 * vol_component) + (1.8 * move_component) + (1.2 * gap_component) + (1.0 * liq_component)

def recommend_direction(prev_yes: float, yes: float, vol_delta: float, gap_cents: float):
    """
    Clear text: BUY YES / BUY NO / WATCH
    """
    delta = yes - prev_yes

    # If price moving with volume => directional
    if vol_delta >= 800 and abs(delta) >= 0.002:
        if delta > 0:
            return ("BUY YES", f"YES up (+{delta*100:.2f}pp) with volume up")
        else:
            return ("BUY NO", f"YES down ({delta*100:.2f}pp) with volume up")

    # If gap exists => watch/inefficiency
    if gap_cents >= GAP_CENTS_MIN:
        return ("WATCH", f"Gap ≈ {gap_cents:.1f}¢ (inefficiency signal)")

    # Default
    if abs(delta) >= 0.003:
        return ("WATCH", f"Move detected ({delta*100:.2f}pp), waiting volume confirmation")
    return ("WATCH", "Minor signal")

def market_url(m: dict) -> str:
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    cid = m.get("conditionId") or m.get("id") or ""
    if cid:
        return f"https://polymarket.com/market/{cid}"
    return "https://polymarket.com/"

def market_id(m: dict) -> str:
    # stable-ish keys
    return str(m.get("id") or m.get("conditionId") or m.get("slug") or "").strip()

# -----------------------------
# Main
# -----------------------------
def main():
    state = load_state()
    cleanup_state(state)
    save_state(state)

    send("🤖 Bot ON — aggressive alerts restored (still scored + anti-spam).")

    while True:
        t_loop = now_ts()
        alerts_sent_now = 0
        scanned = 0
        binary = 0
        candidates = []

        try:
            cleanup_state(state)

            markets = fetch_markets(PAGES, PAGE_SIZE)

            for m in markets:
                scanned += 1

                mid = market_id(m)
                if not mid:
                    continue

                yes, no = extract_binary_prices(m)
                if yes is None or no is None:
                    continue
                binary += 1

                title = (m.get("question") or m.get("title") or "Market").strip()
                vol = safe_float(m.get("volumeNum") or m.get("volume") or 0.0)
                liq = safe_float(m.get("liquidityNum") or m.get("liquidity") or 0.0)

                snap = state["markets"].get(mid, {})
                prev_vol = safe_float(snap.get("vol", 0.0))
                prev_yes = safe_float(snap.get("yes", yes))

                vol_delta = max(0.0, vol - prev_vol)
                price_move_abs = abs(yes - prev_yes)
                gap_cents = compute_gap_cents(yes, no)

                s = score_market(vol_delta, price_move_abs, gap_cents, liq)

                # Keep top candidates for fallback (even if we don't alert)
                candidates.append((s, mid, title, yes, no, vol_delta, liq, gap_cents, prev_yes, m))

                # Per-market cooldown
                last_alert = int(snap.get("last_alert", 0))
                cooldown_ok = (now_ts() - last_alert) >= (COOLDOWN_MINUTES * 60)

                # Aggressive triggers (far easier than before)
                gap_ok = gap_cents >= GAP_CENTS_MIN
                move_ok = price_move_abs >= 0.002
                vol_ok = vol_delta >= 800

                should_alert = cooldown_ok and (s >= SCORE_MIN) and (gap_ok or (move_ok and vol_ok) or (vol_delta >= 5000) or (price_move_abs >= 0.006))

                if should_alert and can_send_alert(state):
                    action, reason = recommend_direction(prev_yes=prev_yes, yes=yes, vol_delta=vol_delta, gap_cents=gap_cents)
                    msg = (
                        f"🚨 ALERTA | Score {s:.2f}\n"
                        f"🎯 AÇÃO: {action}\n"
                        f"🧠 Motivo: {reason}\n"
                        f"📌 Gap: {gap_cents:.1f}¢ | VolΔ: {vol_delta:.0f} | Liq: {liq:.0f}\n"
                        f"YES: {yes:.3f} | NO: {no:.3f}\n"
                        f"{title[:160]}\n"
                        f"{market_url(m)}"
                    )
                    send(msg)
                    alerts_sent_now += 1
                    record_alert(state)

                    snap["last_alert"] = now_ts()

                # Update snapshot
                snap.update({
                    "vol": vol,
                    "yes": yes,
                    "no": no,
                    "liq": liq,
                    "last_seen": now_ts(),
                    "title": title[:160]
                })
                state["markets"][mid] = snap

            # Fallback if no alerts for a while (prevents “12h silence” forever)
            t = now_ts()
            last_any = int(state.get("last_any_alert", 0))
            last_fallback = int(state.get("last_fallback", 0))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)

            no_alert_too_long = (last_any == 0) or ((t - last_any) >= NO_ALERT_FALLBACK_MINUTES * 60)
            fallback_cooldown_ok = (t - last_fallback) >= (NO_ALERT_FALLBACK_MINUTES * 60)

            if alerts_sent_now == 0 and no_alert_too_long and fallback_cooldown_ok and candidates:
                topn = candidates[:max(1, FALLBACK_TOPN)]
                lines = []
                for i, (s, mid, title, yes, no, vol_delta, liq, gap_cents, prev_yes, m) in enumerate(topn, start=1):
                    action, reason = recommend_direction(prev_yes=prev_yes, yes=yes, vol_delta=vol_delta, gap_cents=gap_cents)
                    lines.append(
                        f"{i}) Score {s:.2f} | {action} | Gap {gap_cents:.1f}¢ | VolΔ {vol_delta:.0f} | YES {yes:.3f}\n"
                        f"   {title[:120]}\n"
                        f"   {market_url(m)}"
                    )

                send("🟣 Fallback (sem alertas recentes) — Top oportunidades agora:\n\n" + "\n\n".join(lines))
                state["last_fallback"] = t  # do not spam fallback

            # Heartbeat
            last_hb = int(state.get("last_heartbeat", 0))
            if (now_ts() - last_hb) >= HEARTBEAT_MINUTES * 60:
                last_alert_time = int(state.get("last_any_alert", 0))
                last_alert_str = "never" if last_alert_time == 0 else utc_iso(last_alert_time)
                send(
                    f"🟣 Heartbeat: scanned {scanned} | binary {binary} | candidates {len(candidates)} | alerts now {alerts_sent_now}\n"
                    f"Last alert (UTC): {last_alert_str}"
                )
                state["last_heartbeat"] = now_ts()

            save_state(state)

        except requests.HTTPError as e:
            send(f"⚠️ API HTTP error: {str(e)[:180]}")
        except Exception as e:
            send(f"⚠️ Bot error: {type(e).__name__}: {str(e)[:180]}")

        # Sleep
        elapsed = now_ts() - t_loop
        time.sleep(max(10, POLL_SECONDS - elapsed))

if __name__ == "__main__":
    main()
