#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import math
import hashlib
from datetime import datetime, timezone

import requests
import telebot

# =========================
# ENV VARS (Railway)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars.")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# =========================
# POLYMARKET ENDPOINTS
# =========================
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# =========================
# BOT SETTINGS (safe defaults)
# =========================
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))  # how often to scan
MARKET_LIMIT = int(os.getenv("MARKET_LIMIT", "80"))  # how many markets to scan per loop

# Filters (tune without editing code)
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "20000"))  # minimum liquidity
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "20000"))        # minimum volume (if available)
MIN_SPREAD = float(os.getenv("MIN_SPREAD", "0.02"))         # 2 cents spread

# Anti-spam
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "1800"))  # 30 min per same alert

# Requests
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Store last-sent alerts
last_sent = {}  # key -> unix timestamp


# =========================
# HELPERS
# =========================
def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def safe_float(x, default=0.0):
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


def send(msg: str):
    bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)


def http_get_json(url, params=None, headers=None):
    err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            time.sleep(0.8 * attempt)
    raise err


def get_markets():
    # Active, not closed. Gamma is documented as /markets with filters.
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(MARKET_LIMIT),
        "offset": "0",
    }
    return http_get_json(f"{GAMMA_BASE}/markets", params=params)


def get_best_prices(token_id: str):
    # CLOB /price: side=buy is the price to buy (best ask),
    # side=sell is the price to sell (best bid).
    # (If one side fails, we treat it as missing.)
    ask = None
    bid = None

    try:
        j = http_get_json(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "buy"})
        ask = safe_float(j.get("price"), None)
    except Exception:
        pass

    try:
        j = http_get_json(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "sell"})
        bid = safe_float(j.get("price"), None)
    except Exception:
        pass

    return bid, ask


def should_send(key: str) -> bool:
    t = time.time()
    last = last_sent.get(key, 0)
    if t - last >= COOLDOWN_SECONDS:
        last_sent[key] = t
        # keep dict from growing forever
        if len(last_sent) > 2000:
            # drop oldest-ish by rebuilding
            items = sorted(last_sent.items(), key=lambda kv: kv[1], reverse=True)[:1200]
            last_sent.clear()
            last_sent.update(dict(items))
        return True
    return False


def mk_key(*parts):
    raw = "||".join([str(p) for p in parts])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fmt_pct(x):
    try:
        return f"{x*100:.2f}%"
    except Exception:
        return "n/a"


# =========================
# MAIN SCAN LOGIC
# =========================
def scan_once():
    markets = get_markets()
    alerts = []

    for m in markets:
        # Basic fields from Gamma
        market_id = m.get("id")
        question = (m.get("question") or "").strip()
        slug = (m.get("slug") or "").strip()
        liquidity = safe_float(m.get("liquidity"), 0.0)
        volume = safe_float(m.get("volume"), safe_float(m.get("volume24hr"), 0.0))
        end_date = m.get("endDate")

        # Token IDs used to query prices (Gamma returns clobTokenIds)
        clob_ids = m.get("clobTokenIds") or []
        if not isinstance(clob_ids, list) or len(clob_ids) < 1:
            continue

        # Heuristic: first token is usually YES (depends on market),
        # but for “stable” spread detection it still works as a proxy.
        token_id = str(clob_ids[0])

        # Filter by liquidity/volume (avoid dead markets)
        if liquidity < MIN_LIQUIDITY:
            continue
        if volume < MIN_VOLUME:
            continue

        bid, ask = get_best_prices(token_id)
        if bid is None or ask is None:
            continue

        spread = ask - bid
        if spread < MIN_SPREAD:
            continue

        mid = (ask + bid) / 2.0 if (ask is not None and bid is not None) else None
        spread_pct = (spread / mid) if (mid and mid > 0) else None

        url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com/"

        key = mk_key(market_id, round(bid, 4), round(ask, 4))
        if not should_send(key):
            continue

        alerts.append({
            "question": question or f"Market {market_id}",
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_pct": spread_pct,
            "liq": liquidity,
            "vol": volume,
            "end": end_date,
            "url": url,
        })

    # Sort best opportunities first (largest spread)
    alerts.sort(key=lambda a: a["spread"], reverse=True)
    return alerts[:8]


def run_forever():
    send(f"🤖 Bot online ({now_utc_iso()}). Scanning Polymarket spreads…")

    while True:
        try:
            alerts = scan_once()
            if alerts:
                for a in alerts:
                    msg = (
                        "🚨 SPREAD ALERT\n"
                        f"🧠 {a['question']}\n"
                        f"Bid(sell): {a['bid']:.3f} | Ask(buy): {a['ask']:.3f}\n"
                        f"Spread: {a['spread']:.3f}"
                        + (f" ({fmt_pct(a['spread_pct'])})" if a.get("spread_pct") is not None else "")
                        + "\n"
                        f"Liq: {a['liq']:.0f} | Vol: {a['vol']:.0f}\n"
                        f"🔗 {a['url']}\n\n"
                        "Note: This is a market-structure alert (wide bid/ask). "
                        "Consider checking the orderbook and using limit orders. "
                        "Not financial advice."
                    )
                    send(msg)
            # If nothing found, stay quiet (no spam)
        except Exception as e:
            # Never crash — just report occasionally
            err_key = mk_key("err", str(type(e)), str(e)[:120])
            if should_send(err_key):
                send(f"⚠️ Bot error (kept running): {type(e).__name__}: {str(e)[:220]}")
        finally:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_forever()
