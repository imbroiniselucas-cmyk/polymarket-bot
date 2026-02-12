#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import urllib.request
import urllib.parse
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_SECONDS = 600
MIN_SCORE = 15
MAX_SPREAD = 0.045
MIN_LIQUIDITY = 2000

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com/price"

STATE = {}

# =========================
# SIMPLE HTTP GET
# =========================
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "polybot"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode())

def http_post(url, data):
    data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as response:
        return response.read()

# =========================
# TELEGRAM
# =========================
def tg_send(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Missing TELEGRAM config")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": False
    }
    try:
        http_post(url, payload)
    except Exception as e:
        print("Telegram error:", e)

# =========================
# PRICE
# =========================
def get_price(token_id, side):
    url = f"{CLOB}?token_id={token_id}&side={side}"
    try:
        data = http_get(url)
        return float(data.get("price"))
    except:
        return None

# =========================
# SCAN
# =========================
def scan():
    print("Scanning markets...")
    try:
        markets = http_get(GAMMA + "?limit=200&active=true")
    except Exception as e:
        print("Fetch error:", e)
        return

    for m in markets:
        if not m.get("active"):
            continue

        liquidity = float(m.get("liquidity", 0))
        if liquidity < MIN_LIQUIDITY:
            continue

        token_ids = m.get("clobTokenIds")
        if not token_ids or len(token_ids) < 2:
            continue

        ask1 = get_price(token_ids[0], "buy")
        ask2 = get_price(token_ids[1], "buy")

        if not ask1 or not ask2:
            continue

        spread = abs(ask1 - ask2)
        if spread > MAX_SPREAD:
            continue

        edge = 1 - (ask1 + ask2)
        score = edge * 1000

        if score > MIN_SCORE:
            message = f"""🚨 BUY ARBITRAGE
🧠 Edge: {edge*100:.2f}%
💧 Liquidity: ${int(liquidity)}
⭐ Score: {score:.1f}

{m.get("question")}
https://polymarket.com/market/{m.get("slug")}
"""
            tg_send(message)

# =========================
# HEALTH
# =========================
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def health():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("BOT STARTED")
    threading.Thread(target=health, daemon=True).start()
    tg_send("✅ BOT ONLINE (no requests version)")
    while True:
        try:
            scan()
        except Exception as e:
            print("Scan error:", e)
        time.sleep(SCAN_SECONDS)
