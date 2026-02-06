#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import hashlib
from typing import Optional, Tuple
import requests

# ==========================
# ENV
# ==========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()

MIN_SCORE = float(os.getenv("MIN_SCORE", "30"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))  # 5 min
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "6"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "45"))

MOVE_VOL_MIN = float(os.getenv("MOVE_VOL_MIN", "800"))
MOMENTUM_MIN = float(os.getenv("MOMENTUM_MIN", "0.003"))

SPREAD_SOFT_MAX = float(os.getenv("SPREAD_SOFT_MAX", "0.12"))
SPREAD_HARD_MAX = float(os.getenv("SPREAD_HARD_MAX", "0.22"))

HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "60"))

STATE_FILE = "state.json"

# ==========================
# TELEGRAM
# ==========================
def tg_send(text: str) -> bool:
    """Returns True if message was accepted by Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        print("ENV TELEGRAM_TOKEN len:", len(TELEGRAM_TOKEN))
        print("ENV TELEGRAM_CHAT_ID:", repr(TELEGRAM_CHAT_ID))
        print("Message that would be sent:\n", text)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            print("❌ Telegram HTTP", r.status_code)
            print("Response:", r.text[:800])
            return False
        return True
    except Exception as e:
        print("❌ Telegram exception:", repr(e))
        return False

# ==========================
# FETCH MARKETS
# ==========================
def fetch_markets() -> list[dict]:
    if not POLY_ENDPOINT:
        raise RuntimeError("POLY_ENDPOINT not set. Set it to your working JSON endpoint.")
    r = requests.get(POLY_ENDPOINT, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and isinstance(data.get("markets"), list):
        return data["markets"]
    if isinstance(data, list):
        return data
    raise RuntimeError("Unexpected POLY_ENDPOINT response. Expected list or {'markets':[...]}")

# ==========================
# HELPERS
# ==========================
def _get(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default

def market_url(m: dict) -> str:
    return str(_get(m, "url", "marketUrl", "link", default="")).strip()

def market_title(m: dict) -> str:
    return str(_get(m, "question", "title", "name", default="(untitled)")).strip()

def market_id(m: dict) -> str:
    mid = _get(m, "id", "marketId", "slug", default=None)
    if mid is not None and str(mid).strip():
        return str(mid)
    base = (market_url(m) or market_title(m)).encode("utf-8", "ignore")
    return hashlib.sha1(base).hexdigest()[:12]

def get_liq(m: dict) -> float:
    return float(_get(m, "liquidity", "liq", "liquidityUSD", default=0) or 0)

def get_vol24h(m: dict) -> float:
    return float(_get(m, "volume24h", "vol24h", "volume_24h", default=0) or 0)

def get_volume(m: dict) -> float:
    return float(_get(m, "volume", "vol", "volumeUSD", default=0) or 0)

def get_last_price_yes(m: dict) -> Optional[float]:
    direct = _get(m, "yes", "yesPrice", "p_yes", default=None)
    if direct is not None:
        try:
            p = float(direct)
            return p if 0 <= p <= 1 else None
        except:
            pass

    outcomes = _get(m, "outcomes", "tokens", default=None)
    if isinstance(outcomes, list):
        for o in outcomes:
            name = str(_get(o, "name", "outcome", default="")).lower()
            if name in ("yes", "y"):
                try:
                    p = float(_get(o, "price", "prob", "p", default=None))
                    return p if p is not None and 0 <= p <= 1 else None
                except:
                    return None

    prices = _get(m, "prices", default=None)
    if isinstance(prices, dict):
        for k, v in prices.items():
            if str(k).lower() == "yes":
                try:
                    p = float(v)
                    return p if 0 <= p <= 1 else None
                except:
                    return None

    return None

def get_best_bid_ask_yes(m: dict) -> Tuple[Optional[float], Optional[float]]:
    bid = _get(m, "bestBidYes", "bidYes", default=None)
    ask = _get(m, "bestAskYes", "askYes", default=None)
    if bid is not None or ask is not None:
        try:
            return (float(bid) if bid is not None else None,
                    float(ask) if ask is not None else None)
        except:
            return None, None

    ob = _get(m, "orderbook", "orderBook", default=None)
    if isinstance(ob, dict):
        yes_ob = None
        for k in ob.keys():
            if str(k).lower() == "yes":
                yes_ob = ob[k]
                break
        if isinstance(yes_ob, dict):
            try:
                bid = yes_ob.get("bid") or yes_ob.get("bestBid")
                ask = yes_ob.get("ask") or yes_ob.get("bestAsk")
                return (float(bid) if bid is not None else None,
                        float(ask) if ask is not None else None)
            except:
                return None, None

    return None, None

def clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))

def now_ts() -> int:
    return int(time.time())

# ==========================
# STATE
# ==========================
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"seen": {}, "snap": {}, "last_heartbeat_ts": 0}

def save_state(st: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("state save error:", repr(e))

def can_alert(st: dict, mid: str, score: float) -> bool:
    seen = st.get("seen", {}).get(mid)
    if not seen:
        return True
    last_ts = int(seen.get("ts", 0))
    last_score = float(seen.get("score", 0))
    if now_ts() - last_ts >= COOLDOWN_MINUTES * 60:
        return True
    if score >= last_score + 7:
        return True
    return False

def mark_alerted(st: dict, mid: str, score: float) -> None:
    st.setdefault("seen", {})[mid] = {"ts": now_ts(), "score": round(score, 2)}

# ==========================
# SCORING (BUY-ONLY, AGGRESSIVE)
# ==========================
def score_market(m: dict, prev: Optional[dict]) -> tuple[float, dict]:
    liq = get_liq(m)
    vol = get_volume(m)

    p_yes = get_last_price_yes(m)
    bid, ask = get_best_bid_ask_yes(m)

    prev_vol = float(prev.get("volume", 0.0)) if prev else 0.0
    prev_p = float(prev.get("p_yes", p_yes if p_yes is not None else 0.0)) if prev else (p_yes or 0.0)

    vol_delta = max(0.0, vol - prev_vol)
    p_now = p_yes if p_yes is not None else prev_p
    dp = p_now - prev_p
    price_move = abs(dp)

    suggested = "YES" if dp >= 0 else "NO"

    spread = None
    if bid is not None and ask is not None and ask > 0:
        mid = (bid + ask) / 2.0
        spread = (ask - bid) / max(mid, 1e-9)

    if spread is not None and spread > SPREAD_HARD_MAX:
        return 0.0, {"blocked": True, "block_reason": "spread_hard", "id": market_id(m)}

    liq_s = clamp(math.log10(liq + 1) / 6.0, 0, 1)
    vdelta_s = clamp(math.log10(vol_delta + 1) / 4.5, 0, 1)
    move_s = clamp(price_move / 0.04, 0, 1)

    spread_pen = 0.0
    if spread is not None and spread > SPREAD_SOFT_MAX:
        spread_pen = (spread - SPREAD_SOFT_MAX) * 80.0

    trigger_ok = (vol_delta >= MOVE_VOL_MIN) or (price_move >= MOMENTUM_MIN)

    base = 100.0 * (0.45 * vdelta_s + 0.35 * move_s + 0.20 * liq_s) - spread_pen
    score = max(0.0, base if trigger_ok else base * 0.55)

    d = {
        "blocked": False,
        "suggested": suggested,
        "liq": liq,
        "vol": vol,
        "vol_delta": vol_delta,
        "p_yes": p_yes,
        "prev_p": prev_p,
        "dp": dp,
        "bid": bid,
        "ask": ask,
        "spread": spread,
        "url": market_url(m),
        "title": market_title(m),
        "id": market_id(m),
        "trigger_ok": trigger_ok,
    }
    return score, d

def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "-"
    return f"{x*100:.2f}%"

def format_buy_alert(score: float, d: dict) -> str:
    suggested = d.get("suggested", "YES")
    title = d.get("title", "")
    url = d.get("url", "")

    lines = [
        f"🚨 <b>BUY ALERT</b> | Score: <b>{score:.1f}</b>",
        f"🟢 <b>BUY {suggested}</b> (entry)",
        f"📝 <b>{title}</b>",
    ]

    p_yes = d.get("p_yes")
    if p_yes is not None:
        lines.append(f"📌 YES: <b>{p_yes:.3f}</b> (Δ {d.get('dp', 0.0):+.3f})")

    if d.get("bid") is not None and d.get("ask") is not None:
        lines.append(f"📚 Book YES | bid {d['bid']:.3f} / ask {d['ask']:.3f} | spread {fmt_pct(d.get('spread'))}")

    lines.append(f"💧 Liq: <b>{d.get('liq', 0):,.0f}</b> | VolΔ: <b>{d.get('vol_delta', 0):,.0f}</b>")
    lines.append(f"🧠 Reason: aggressive entry (move/flow), buy-only feed")

    if url:
        lines.append(url)

    return "\n".join(lines)

# ==========================
# MAIN
# ==========================
def main():
    st = load_state()
    snap = st.get("snap", {})

    print("BOOT_OK: main.py running")
    print("ENV token?", bool(TELEGRAM_TOKEN), "len", len(TELEGRAM_TOKEN))
    print("ENV chat_id?", bool(TELEGRAM_CHAT_ID), "val", repr(TELEGRAM_CHAT_ID))
    print("ENV poly?", bool(POLY_ENDPOINT), "len", len(POLY_ENDPOINT))

    # 1) BOOT PING (this is the key change)
    tg_send(
        "✅ <b>BOT ON</b>\n"
        f"Mode: <b>BUY only</b>\n"
        f"MinScore: <b>{MIN_SCORE:g}</b>\n"
        f"Poll: <b>{POLL_SECONDS}s</b>\n"
        f"Max/Cycle: <b>{MAX_ALERTS_PER_CYCLE}</b>"
    )

    while True:
        try:
            # 2) HEARTBEAT (1 per hour)
            if now_ts() - int(st.get("last_heartbeat_ts", 0)) >= HEARTBEAT_MINUTES * 60:
                ok = tg_send("💡 <b>Heartbeat</b>: bot alive (waiting for BUY opportunities).")
                if ok:
                    st["last_heartbeat_ts"] = now_ts()

            markets = fetch_markets()
            scored: list[tuple[float, dict]] = []

            for m in markets:
                mid = market_id(m)
                prev = snap.get(mid)

                score, d = score_market(m, prev)

                # update snapshot
                pyes = get_last_price_yes(m)
                snap[mid] = {
                    "volume": get_volume(m),
                    "p_yes": pyes if pyes is not None else (prev.get("p_yes") if prev else 0.0),
                    "ts": now_ts(),
                }

                if score >= MIN_SCORE and not d.get("blocked", False):
                    scored.append((score, d))

            scored.sort(key=lambda x: x[0], reverse=True)

            sent = 0
            for score, d in scored:
                if sent >= MAX_ALERTS_PER_CYCLE:
                    break
                if not can_alert(st, d["id"], score):
                    continue
                tg_send(format_buy_alert(score, d))
                mark_alerted(st, d["id"], score)
                sent += 1

            st["snap"] = snap
            save_state(st)

        except Exception as e:
            print("loop error:", repr(e))

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
