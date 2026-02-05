#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import re
import hashlib
import requests
import telebot
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from urllib.parse import quote_plus

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID env vars.")

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# =========================
# ENDPOINTS
# =========================
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

# =========================
# SETTINGS (tune via Railway Variables)
# =========================
POLL_SECONDS         = int(os.getenv("POLL_SECONDS", "60"))          # loop base
ALERT_EVERY_SECONDS  = int(os.getenv("ALERT_EVERY_SECONDS", "600"))  # 10 min (cadência de alertas por mercado)
HEARTBEAT_SECONDS    = int(os.getenv("HEARTBEAT_SECONDS", "600"))    # 10 min status

MARKET_LIMIT         = int(os.getenv("MARKET_LIMIT", "120"))
MIN_LIQUIDITY        = float(os.getenv("MIN_LIQUIDITY", "15000"))
MIN_VOLUME           = float(os.getenv("MIN_VOLUME", "15000"))
MIN_SPREAD           = float(os.getenv("MIN_SPREAD", "0.015"))       # 1.5¢
MIN_MOVE_ABS         = float(os.getenv("MIN_MOVE_ABS", "0.010"))     # 1.0¢ move
MOVE_LOOKBACK_SEC    = int(os.getenv("MOVE_LOOKBACK_SEC", "900"))    # 15 min

NEWS_ENABLED         = (os.getenv("NEWS_ENABLED", "1").strip() == "1")
NEWS_MAX_ITEMS       = int(os.getenv("NEWS_MAX_ITEMS", "4"))
NEWS_COOLDOWN_SEC    = int(os.getenv("NEWS_COOLDOWN_SEC", "1800"))   # 30 min por mercado
NEWS_QUERY_WORDS     = int(os.getenv("NEWS_QUERY_WORDS", "5"))       # quantas palavras-chave da pergunta

HTTP_TIMEOUT         = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_RETRIES          = int(os.getenv("MAX_RETRIES", "3"))

# =========================
# STATE
# =========================
last_alert_at = {}        # market_id -> ts
last_heartbeat = 0

# price memory: token_id -> [(ts, mid)]
price_hist = {}

# news memory: market_id -> {hashes}
news_seen = {}            # market_id -> set(hash)

# =========================
# HELPERS
# =========================
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

def send(msg: str):
    bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)

def http_get_json(url, params=None, headers=None):
    err = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            err = e
            time.sleep(0.6 * i)
    raise err

def http_get_text(url, params=None, headers=None):
    err = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            err = e
            time.sleep(0.6 * i)
    raise err

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def clean_keywords(question: str):
    # pega palavras relevantes e remove lixo
    q = (question or "").lower()
    q = re.sub(r"[^a-z0-9\s\-\$]", " ", q)
    words = [w for w in q.split() if len(w) >= 4 and w not in {
        "will", "price", "reach", "above", "below", "before", "after", "this",
        "that", "with", "from", "what", "when", "where", "which", "could",
        "would", "should", "february", "january", "march", "april", "2026"
    }]
    # dedupe mantendo ordem
    out = []
    seen = set()
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:NEWS_QUERY_WORDS]

# =========================
# POLYMARKET
# =========================
def get_markets():
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(MARKET_LIMIT),
        "offset": "0",
    }
    return http_get_json(f"{GAMMA_BASE}/markets", params=params)

def get_best_prices(token_id: str):
    # buy -> best ask, sell -> best bid
    bid = None
    ask = None
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

def record_mid(token_id: str, mid: float, ts: float):
    arr = price_hist.get(token_id, [])
    arr.append((ts, mid))
    # keep only last 2 hours
    cutoff = ts - 7200
    arr = [(t, m) for (t, m) in arr if t >= cutoff]
    price_hist[token_id] = arr

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
        # fallback: oldest in window
        past = arr[0][1]
    return arr[-1][1] - past

# =========================
# NEWS (Google News RSS)
# =========================
def fetch_news_rss(query: str):
    # RSS do Google News (sem API key)
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    xml = http_get_text(url)
    root = ET.fromstring(xml)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub = item.findtext("pubDate") or ""
        items.append({"title": title, "link": link, "pubDate": pub})
    return items

def maybe_send_news(market_id: str, question: str):
    if not NEWS_ENABLED:
        return

    # cooldown por mercado
    now = time.time()
    last = last_alert_at.get(f"news::{market_id}", 0)
    if now - last < NEWS_COOLDOWN_SEC:
        return

    kws = clean_keywords(question)
    if not kws:
        return

    query = " ".join(kws)
    try:
        items = fetch_news_rss(query)
    except Exception:
        return

    if not items:
        return

    seen = news_seen.get(market_id, set())
    new_items = []
    for it in items[:10]:
        h = sha1((it["title"] or "") + (it["link"] or ""))
        if h not in seen:
            new_items.append(it)
            seen.add(h)
        if len(new_items) >= NEWS_MAX_ITEMS:
            break

    if not new_items:
        news_seen[market_id] = seen
        return

    news_seen[market_id] = seen
    last_alert_at[f"news::{market_id}"] = now

    lines = [f"🗞️ NEWS (match): {clip(question, 90)}", f"Query: {query}"]
    for it in new_items:
        lines.append(f"• {clip(it['title'], 110)}")
        if it["link"]:
            lines.append(f"  {it['link']}")
    send("\n".join(lines))

# =========================
# SCAN + ALERTS
# =========================
def should_alert(market_id: str):
    now = time.time()
    last = last_alert_at.get(market_id, 0)
    if now - last >= ALERT_EVERY_SECONDS:
        last_alert_at[market_id] = now
        return True
    return False

def heartbeat(total_scanned: int, total_candidates: int):
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_SECONDS:
        last_heartbeat = now
        send(f"✅ Bot vivo ({now_iso()}) | scanned={total_scanned} | candidates={total_candidates} | poll={POLL_SECONDS}s")

def scan_once():
    markets = get_markets()
    total_scanned = 0
    candidates = []

    ts = time.time()

    for m in markets:
        total_scanned += 1

        market_id = str(m.get("id") or "")
        question  = (m.get("question") or "").strip()
        slug      = (m.get("slug") or "").strip()
        liquidity = safe_float(m.get("liquidity"), 0.0) or 0.0
        volume    = safe_float(m.get("volume"), safe_float(m.get("volume24hr"), 0.0)) or 0.0

        clob_ids = m.get("clobTokenIds") or []
        if not market_id or not question or not isinstance(clob_ids, list) or len(clob_ids) < 1:
            continue

        if liquidity < MIN_LIQUIDITY or volume < MIN_VOLUME:
            continue

        token_id = str(clob_ids[0])  # proxy token (geralmente YES)

        bid, ask = get_best_prices(token_id)
        if bid is None or ask is None:
            continue

        mid = (bid + ask) / 2.0
        record_mid(token_id, mid, ts)

        spread = ask - bid
        move = move_over_lookback(token_id, ts, MOVE_LOOKBACK_SEC)
        move_abs = abs(move) if move is not None else 0.0

        # score simples (agressivo, mas com filtro)
        score = 0.0
        if spread >= MIN_SPREAD:
            score += min(3.0, spread / MIN_SPREAD)
        if move_abs >= MIN_MOVE_ABS:
            score += min(3.0, move_abs / MIN_MOVE_ABS)
        # bônus por liquidez/volume
        score += min(2.0, liquidity / (MIN_LIQUIDITY * 4))
        score += min(2.0, volume / (MIN_VOLUME * 4))

        if score < 3.0:
            continue

        url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com/"
        candidates.append({
            "market_id": market_id,
            "question": question,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "move": move,
            "liq": liquidity,
            "vol": volume,
            "score": score,
            "url": url
        })

    # ordenar melhores primeiro
    candidates.sort(key=lambda x: (x["score"], x["spread"]), reverse=True)

    # heartbeat
    heartbeat(total_scanned, len(candidates))

    # alertar top N
    for c in candidates[:6]:
        if not should_alert(c["market_id"]):
            continue

        direction = ""
        if c["move"] is not None:
            if c["move"] > 0:
                direction = "📈 momentum: subindo"
            elif c["move"] < 0:
                direction = "📉 momentum: caindo"
            else:
                direction = "⏸️ momentum: estável"

        msg = (
            "🚨 OPORTUNIDADE\n"
            f"🧠 {clip(c['question'], 140)}\n"
            f"Score: {c['score']:.2f} | Liq: {c['liq']:.0f} | Vol: {c['vol']:.0f}\n"
            f"Bid(sell): {c['bid']:.3f} | Ask(buy): {c['ask']:.3f} | Spread: {c['spread']:.3f}\n"
            f"{direction}\n"
            f"🔗 {c['url']}\n\n"
            "Sugestão prática: olhar o orderbook e considerar LIMIT (spread alto costuma dar edge)."
        )
        send(msg)

        # notícias relacionadas (se habilitado)
        maybe_send_news(c["market_id"], c["question"])

def run():
    send(f"🤖 Bot online ({now_iso()}). Alertas ~a cada 10 min por mercado. News={'ON' if NEWS_ENABLED else 'OFF'}.")
    while True:
        try:
            scan_once()
        except Exception as e:
            # não derruba o bot
            send(f"⚠️ Erro (mas sigo rodando): {type(e).__name__}: {str(e)[:200]}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    run()
