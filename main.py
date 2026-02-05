#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import re
import hashlib
import threading
from datetime import datetime, timezone
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import telebot

# ======================================================
# HEALTH SERVER (Railway Web)
# ======================================================
def start_health():
    port = int(os.getenv("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            return

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=start_health, daemon=True).start()

# ======================================================
# ENV
# ======================================================
def env(k): 
    return (os.getenv(k) or "").strip()

TELEGRAM_TOKEN   = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

print("🤖 Bot booting...", flush=True)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID", flush=True)
    while True:
        time.sleep(60)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# ======================================================
# SETTINGS (Railway Variables)
# ======================================================
POLL_SECONDS        = int(env("POLL_SECONDS") or 60)
ALERT_EVERY_SECONDS = int(env("ALERT_EVERY_SECONDS") or 600)   # 10 min
HEARTBEAT_SECONDS   = int(env("HEARTBEAT_SECONDS") or 600)

MARKET_LIMIT  = int(env("MARKET_LIMIT") or 140)
MIN_LIQUIDITY = float(env("MIN_LIQUIDITY") or 15000)
MIN_VOLUME    = float(env("MIN_VOLUME") or 15000)
MIN_SPREAD    = float(env("MIN_SPREAD") or 0.015)
MIN_MOVE_ABS  = float(env("MIN_MOVE_ABS") or 0.010)
LOOKBACK_SEC  = int(env("LOOKBACK_SEC") or 900)

NEWS_ENABLED  = (env("NEWS_ENABLED") or "1") == "1"

# ======================================================
# ENDPOINTS
# ======================================================
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

# ======================================================
# STATE
# ======================================================
last_alert = {}
price_hist = {}
last_heartbeat = 0

# ======================================================
# HELPERS
# ======================================================
def now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

def send(msg):
    bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)

def get_json(url, params=None):
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()

# ======================================================
# POLYMARKET
# ======================================================
def get_markets():
    return get_json(f"{GAMMA}/markets", {
        "active": "true",
        "closed": "false",
        "limit": MARKET_LIMIT,
        "offset": 0
    })

def get_prices(token):
    bid = ask = None
    try:
        bid = float(get_json(f"{CLOB}/price", {"token_id": token, "side": "sell"})["price"])
        ask = float(get_json(f"{CLOB}/price", {"token_id": token, "side": "buy"})["price"])
    except:
        pass
    return bid, ask

def record_price(token, mid):
    arr = price_hist.get(token, [])
    arr.append((time.time(), mid))
    price_hist[token] = [(t, p) for (t, p) in arr if t > time.time() - 7200]

def price_move(token):
    arr = price_hist.get(token, [])
    if len(arr) < 2:
        return None
    return arr[-1][1] - arr[0][1]

# ======================================================
# NEWS (Google RSS)
# ======================================================
def get_news(query):
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    xml = requests.get(url, timeout=10).text
    root = ET.fromstring(xml)
    out = []
    for item in root.findall(".//item")[:3]:
        out.append(item.findtext("title"))
    return out

# ======================================================
# SCAN
# ======================================================
def scan():
    global last_heartbeat

    markets = get_markets()
    scanned = 0
    found = 0

    for m in markets:
        scanned += 1
        q = (m.get("question") or "").strip()
        slug = m.get("slug")
        liq = float(m.get("liquidity") or 0)
        vol = float(m.get("volume") or m.get("volume24hr") or 0)

        if liq < MIN_LIQUIDITY or vol < MIN_VOLUME:
            continue

        tokens = m.get("clobTokenIds") or []
        if not tokens:
            continue

        bid, ask = get_prices(tokens[0])
        if not bid or not ask:
            continue

        spread = ask - bid
        mid = (bid + ask) / 2
        record_price(tokens[0], mid)
        move = price_move(tokens[0])
        move_abs = abs(move) if move else 0

        if spread < MIN_SPREAD and move_abs < MIN_MOVE_ABS:
            continue

        found += 1
        now_ts = time.time()
        if now_ts - last_alert.get(m["id"], 0) < ALERT_EVERY_SECONDS:
            continue

        last_alert[m["id"]] = now_ts

        direction = "📈 subindo" if move and move > 0 else "📉 caindo" if move and move < 0 else "⏸️"

        msg = (
            "🚨 OPORTUNIDADE\n"
            f"🧠 {q}\n"
            f"Liq: {liq:.0f} | Vol: {vol:.0f}\n"
            f"Bid: {bid:.3f} | Ask: {ask:.3f} | Spread: {spread:.3f}\n"
            f"Momentum: {direction}\n"
            f"🔗 https://polymarket.com/market/{slug}"
        )
        send(msg)

        if NEWS_ENABLED:
            try:
                news = get_news(q)
                if news:
                    send("🗞️ NEWS:\n" + "\n".join(f"• {n}" for n in news))
            except:
                pass

    if time.time() - last_heartbeat > HEARTBEAT_SECONDS:
        last_heartbeat = time.time()
        send(f"✅ Bot vivo {now()} | scanned={scanned} | hits={found}")

# ======================================================
# MAIN LOOP
# ======================================================
send("🤖 Bot online e ATIVO. Monitorando oportunidades.")

while True:
    try:
        scan()
    except Exception as e:
        send(f"⚠️ Erro: {type(e).__name__}: {str(e)[:120]}")
    time.sleep(POLL_SECONDS)
