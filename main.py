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

# ========= 0) HTTP server mínimo pro Railway (WEB) =========
def start_health_server():
    port = int(os.getenv("PORT", "8080"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format, *args):
            # silenciar logs HTTP
            return

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[health] listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()

threading.Thread(target=start_health_server, daemon=True).start()

# ========= 1) ENV =========
def env_get(key: str) -> str:
    v = os.getenv(key)
    return "" if v is None else v.strip()

TELEGRAM_TOKEN = env_get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env_get("TELEGRAM_CHAT_ID")

print("BOOT_OK: main.py running", flush=True)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.", flush=True)
    print("ENV has TELEGRAM_TOKEN?", "TELEGRAM_TOKEN" in os.environ, "LEN:", len(TELEGRAM_TOKEN), flush=True)
    print("ENV has TELEGRAM_CHAT_ID?", "TELEGRAM_CHAT_ID" in os.environ, "VAL:", repr(TELEGRAM_CHAT_ID), flush=True)
    print("TELE/CHAT keys:", [k for k in os.environ.keys() if "TELE" in k or "CHAT" in k], flush=True)
    # não crasha: fica vivo
    while True:
        time.sleep(60)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# ========= 2) SETTINGS =========
def env_int(key: str, default: int) -> int:
    try:
        return int(env_get(key) or default)
    except Exception:
        return default

def env_float(key: str, default: float) -> float:
    try:
        return float(env_get(key) or default)
    except Exception:
        return default

POLL_SECONDS        = env_int("POLL_SECONDS", 60)
ALERT_EVERY_SECONDS = env_int("ALERT_EVERY_SECONDS", 600)   # 10 min
HEARTBEAT_SECONDS   = env_int("HEARTBEAT_SECONDS", 600)     # 10 min

MARKET_LIMIT        = env_int("MARKET_LIMIT", 140)
MIN_LIQUIDITY       = env_float("MIN_LIQUIDITY", 15000.0)
MIN_VOLUME          = env_float("MIN_VOLUME", 15000.0)
MIN_SPREAD          = env_float("MIN_SPREAD", 0.015)
MIN_MOVE_ABS        = env_float("MIN_MOVE_ABS", 0.010)
MOVE_LOOKBACK_SEC   = env_int("MOVE_LOOKBACK_SEC", 900)

NEWS_ENABLED        = (env_get("NEWS_ENABLED") or "1") == "1"
NEWS_MAX_ITEMS      = env_int("NEWS_MAX_ITEMS", 3)
NEWS_COOLDOWN_SEC   = env_int("NEWS_COOLDOWN_SEC", 1800)
NEWS_QUERY_WORDS    = env_int("NEWS_QUERY_WORDS", 5)

HTTP_TIMEOUT        = env_int("HTTP_TIMEOUT", 15)
MAX_RETRIES         = env_int("MAX_RETRIES", 3)

# ========= 3) ENDPOINTS =========
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

# ========= 4) STATE =========
last_alert_at = {}
last_heartbeat = 0
price_hist = {}
news_seen = {}

# ========= 5) HELPERS =========
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def safe_float(x, default=None):
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if not s:
            return default
        return float(s)
    except Exception:
        return default

def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def send(msg: str):
    bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)

def http_get_json(url, params=None):
    err = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            time.sleep(0.6 * i)
    raise err

def http_get_text(url, params=None):
    err = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            err = e
            time.sleep(0.6 * i)
    raise err

# ========= 6) POLYMARKET =========
def get_markets():
    params = {"active": "true", "closed": "false", "limit": str(MARKET_LIMIT), "offset": "0"}
    return http_get_json(f"{GAMMA_BASE}/markets", params=params)

def get_best_prices(token_id: str):
    bid = ask = None
    try:
        j = http_get_json(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "sell"})
        bid = safe_float(j.get("price"), None)
    except Exception:
        pass
    try:
        j = http_get_json(f"{CLOB_BASE}/price", params={"token_id": token_id, "side": "buy"})
        ask = safe_float(j.get("price"), None)
    except Exception:
        pass
    return bid, ask

def record_mid(token_id: str, ts: float, mid: float):
    arr = price_hist.get(token_id, [])
    arr.append((ts, mid))
    cutoff = ts - 7200
    price_hist[token_id] = [(t, m) for (t, m) in arr if t >= cutoff]

def move_over_lookback(token_id: str, ts: float, lookback: int):
    arr = price_hist.get(token_id, [])
    if len(arr) < 2:
        return None
    target = ts - lookback
    past = None
    for (t, m) in arr:
        if t <= target:
            past = m
    if past is None:
        past = arr[0][1]
    return arr[-1][1] - past

# ========= 7) NEWS (RSS) =========
def clean_keywords(question: str):
    q = (question or "").lower()
    q = re.sub(r"[^a-z0-9\s\-\$]", " ", q)
    words = [w for w in q.split() if len(w) >= 4]
    out, seen = [], set()
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:NEWS_QUERY_WORDS]

def fetch_news_rss(query: str):
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    xml = http_get_text(url)
    root = ET.fromstring(xml)
    items = []
    for item in root.findall(".//item"):
        items.append({
            "title": item.findtext("title") or "",
            "link": item.findtext("link") or "",
        })
    return items

def maybe_send_news(market_id: str, question: str):
    if not NEWS_ENABLED:
        return
    now = time.time()
    cd_key = f"news::{market_id}"
    if now - last_alert_at.get(cd_key, 0) < NEWS_COOLDOWN_SEC:
        return
    kws = clean_keywords(question)
    if not kws:
        return
    query = " ".join(kws)
    try:
        items = fetch_news_rss(query)
    except Exception:
        return
    seen = news_seen.get(market_id, set())
    new_items = []
    for it in items[:12]:
        h = sha1(it["title"] + it["link"])
        if h not in seen:
            seen.add(h)
            new_items.append(it)
        if len(new_items) >= NEWS_MAX_ITEMS:
            break
    if not new_items:
        news_seen[market_id] = seen
        return
    news_seen[market_id] = seen
    last_alert_at[cd_key] = now
    lines = [f"🗞️ NEWS: {clip(question, 90)}", f"Query: {query}"]
    for it in new_items:
        lines.append(f"• {clip(it['title'], 110)}")
        if it["link"]:
            lines.append(f"  {it['link']}")
    send("\n".join(lines))

# ========= 8) ALERTS =========
def should_alert(market_id: str):
    now = time.time()
    if now - last_alert_at.get(market_id, 0) >= ALERT_EVERY_SECONDS:
        last_alert_at[market_id] = now
        return True
    return False

def heartbeat(scanned: int, candidates: int):
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_SECONDS:
        last_heartbeat = now
        send(f"✅ Bot vivo ({now_iso()}) | scanned={scanned} | candidates={candidates}")

def scan_once():
    markets = get_markets()
    ts = time.time()
    scanned = 0
    cands = []

    for m in markets:
        scanned += 1
        market_id = str(m.get("id") or "")
        question = (m.get("question") or "").strip()
        slug = (m.get("slug") or "").strip()
        liq = safe_float(m.get("liquidity"), 0.0) or 0.0
        vol = safe_float(m.get("volume"), safe_float(m.get("volume24hr"), 0.0)) or 0.0
        clob_ids = m.get("clobTokenIds") or []
        if not market_id or not question or not isinstance(clob_ids, list) or len(clob_ids) < 1:
            continue
        if liq < MIN_LIQUIDITY or vol < MIN_VOLUME:
            continue

        token_id = str(clob_ids[0])
        bid, ask = get_best_prices(token_id)
        if bid is None or ask is None:
            continue

        mid = (bid + ask) / 2.0
        record_mid(token_id, ts, mid)

        spread = ask - bid
        mv = move_over_lookback(token_id, ts, MOVE_LOOKBACK_SEC)
        mv_abs = abs(mv) if mv is not None else 0.0

        score = 0.0
        if spread >= MIN_SPREAD:
            score += min(3.0, spread / MIN_SPREAD)
        if mv_abs >= MIN_MOVE_ABS:
            score += min(3.0, mv_abs / MIN_MOVE_ABS)
        score += min(2.0, liq / (MIN_LIQUIDITY * 4.0))
        score += min(2.0, vol / (MIN_VOLUME * 4.0))

        if score < 3.0:
            continue

        url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com/"
        cands.append((score, spread, mv, bid, ask, liq, vol, question, url, market_id))

    cands.sort(reverse=True, key=lambda x: (x[0], x[1]))
    heartbeat(scanned, len(cands))

    for (score, spread, mv, bid, ask, liq, vol, question, url, market_id) in cands[:6]:
        if not should_alert(market_id):
            continue

        direction = ""
        if mv is not None:
            direction = "📈 subindo" if mv > 0 else ("📉 caindo" if mv < 0 else "⏸️ estável")

        send(
            "🚨 OPORTUNIDADE\n"
            f"🧠 {clip(question, 140)}\n"
            f"Score: {score:.2f} | Liq: {liq:.0f} | Vol: {vol:.0f}\n"
            f"Bid: {bid:.3f} | Ask: {ask:.3f} | Spread: {spread:.3f}\n"
            f"Momentum: {direction}\n"
            f"🔗 {url}\n\n"
            "Dica: se spread está alto, use LIMIT e olhe orderbook."
        )

        maybe_send_news(market_id, question)

def run():
    send(f"🤖 Bot online ({now_iso()}) | heartbeat 10min | alert 10min/market | news={'ON' if NEWS_ENABLED else 'OFF'}")
    while True:
        try:
            scan_once()
        except Exception as e:
            send(f"⚠️ Erro (sigo rodando): {type(e).__name__}: {str(e)[:180]}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    run()
