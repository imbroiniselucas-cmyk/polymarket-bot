#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

import requests
import telebot

# =========================================================
# 0) BOOT (prova de que o código novo subiu)
# =========================================================
BOOT_TAG = "BOOT_OK_v1.0"
print(f"=== {BOOT_TAG} | main.py loaded ===")

# =========================================================
# 1) ENV (NUNCA CRASHA se faltar vars)
# =========================================================
def env_get(key: str) -> str:
    v = os.getenv(key)
    return "" if v is None else v.strip()

TELEGRAM_TOKEN = env_get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env_get("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
    print("ENV has TELEGRAM_TOKEN?", "TELEGRAM_TOKEN" in os.environ, "LEN:", len(TELEGRAM_TOKEN))
    print("ENV has TELEGRAM_CHAT_ID?", "TELEGRAM_CHAT_ID" in os.environ, "VAL:", repr(TELEGRAM_CHAT_ID))
    print("TELE/CHAT keys visible:", [k for k in os.environ.keys() if "TELE" in k or "CHAT" in k])
    print("👉 Fix: Railway -> Service -> Variables (Production/Preview) + Redeploy.")
    # fica vivo para você ver logs (não crasha em loop)
    while True:
        time.sleep(60)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

# =========================================================
# 2) SETTINGS (tune via Railway Variables)
# =========================================================
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
ALERT_EVERY_SECONDS = env_int("ALERT_EVERY_SECONDS", 600)   # 10 min por mercado
HEARTBEAT_SECONDS   = env_int("HEARTBEAT_SECONDS", 600)     # 10 min status

MARKET_LIMIT        = env_int("MARKET_LIMIT", 140)

MIN_LIQUIDITY       = env_float("MIN_LIQUIDITY", 15000.0)
MIN_VOLUME          = env_float("MIN_VOLUME", 15000.0)
MIN_SPREAD          = env_float("MIN_SPREAD", 0.015)        # 1.5¢
MIN_MOVE_ABS        = env_float("MIN_MOVE_ABS", 0.010)      # 1.0¢
MOVE_LOOKBACK_SEC   = env_int("MOVE_LOOKBACK_SEC", 900)     # 15 min

NEWS_ENABLED        = (env_get("NEWS_ENABLED") or "1") == "1"
NEWS_MAX_ITEMS      = env_int("NEWS_MAX_ITEMS", 3)
NEWS_COOLDOWN_SEC   = env_int("NEWS_COOLDOWN_SEC", 1800)
NEWS_QUERY_WORDS    = env_int("NEWS_QUERY_WORDS", 5)

HTTP_TIMEOUT        = env_int("HTTP_TIMEOUT", 15)
MAX_RETRIES         = env_int("MAX_RETRIES", 3)

# =========================================================
# 3) ENDPOINTS
# =========================================================
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE  = "https://clob.polymarket.com"

# =========================================================
# 4) STATE
# =========================================================
last_alert_at = {}     # market_id -> ts
last_heartbeat = 0

price_hist = {}        # token_id -> [(ts, mid)]
news_seen = {}         # market_id -> set(hash)

# =========================================================
# 5) HELPERS
# =========================================================
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

# =========================================================
# 6) POLYMARKET DATA
# =========================================================
def get_markets():
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(MARKET_LIMIT),
        "offset": "0",
    }
    return http_get_json(f"{GAMMA_BASE}/markets", params=params)

def get_best_prices(token_id: str):
    # sell -> best bid ; buy -> best ask
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

def record_mid(token_id: str, ts: float, mid: float):
    arr = price_hist.get(token_id, [])
    arr.append((ts, mid))
    cutoff = ts - 7200  # 2h
    arr = [(t, m) for (t, m) in arr if t >= cutoff]
    price_hist[token_id] = arr

def move_over_lookback(token_id: str, ts: float, lookback_sec: int):
    arr = price_hist.get(token_id, [])
    if len(arr) < 2:
        return None
    target = ts - lookback_sec
    past = None
    for (t, m) in arr:
        if t <= target:
            past = m
    if past is None:
        past = arr[0][1]
    return arr[-1][1] - past

# =========================================================
# 7) NEWS (Google News RSS)
# =========================================================
def clean_keywords(question: str):
    q = (question or "").lower()
    q = re.sub(r"[^a-z0-9\s\-\$]", " ", q)
    words = [w for w in q.split() if len(w) >= 4 and w not in {
        "will", "price", "reach", "above", "below", "before", "after", "today",
        "tomorrow", "yesterday", "between", "within", "through", "until",
        "february", "january", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december", "2024", "2025", "2026",
        "polymarket"
    }]
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
        title = item.findtext("title") or ""
        link = item.findtext("link") or ""
        pub  = item.findtext("pubDate") or ""
        items.append({"title": title, "link": link, "pubDate": pub})
    return items

def maybe_send_news(market_id: str, question: str):
    if not NEWS_ENABLED:
        return

    now = time.time()
    cd_key = f"news::{market_id}"
    last = last_alert_at.get(cd_key, 0)
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
    for it in items[:12]:
        h = sha1((it["title"] or "") + (it["link"] or ""))
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

# =========================================================
# 8) ALERT LOGIC
# =========================================================
def should_alert(market_id: str) -> bool:
    now = time.time()
    last = last_alert_at.get(market_id, 0)
    if now - last >= ALERT_EVERY_SECONDS:
        last_alert_at[market_id] = now
        return True
    return False

def heartbeat(scanned: int, candidates: int):
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_SECONDS:
        last_heartbeat = now
        send(f"✅ Bot vivo ({now_iso()}) | scanned={scanned} | candidates={candidates} | poll={POLL_SECONDS}s")

def score_candidate(spread: float, move_abs: float, liq: float, vol: float) -> float:
    score = 0.0
    if spread >= MIN_SPREAD:
        score += min(3.0, spread / MIN_SPREAD)
    if move_abs >= MIN_MOVE_ABS:
        score += min(3.0, move_abs / MIN_MOVE_ABS)
    score += min(2.0, liq / (MIN_LIQUIDITY * 4.0))
    score += min(2.0, vol / (MIN_VOLUME * 4.0))
    return score

def scan_once():
    markets = get_markets()
    ts = time.time()
    scanned = 0
    candidates = []

    for m in markets:
        scanned += 1

        market_id = str(m.get("id") or "")
        question  = (m.get("question") or "").strip()
        slug      = (m.get("slug") or "").strip()

        liquidity = safe_float(m.get("liquidity"), 0.0) or 0.0
        volume = safe_float(m.get("volume"), safe_float(m.get("volume24hr"), 0.0)) or 0.0

        clob_ids = m.get("clobTokenIds") or []
        if not market_id or not question or not isinstance(clob_ids, list) or len(clob_ids) < 1:
            continue

        if liquidity < MIN_LIQUIDITY or volume < MIN_VOLUME:
            continue

        token_id = str(clob_ids[0])  # proxy (geralmente YES)

        bid, ask = get_best_prices(token_id)
        if bid is None or ask is None:
            continue

        mid = (bid + ask) / 2.0
        record_mid(token_id, ts, mid)

        spread = ask - bid
        mv = move_over_lookback(token_id, ts, MOVE_LOOKBACK_SEC)
        move_abs = abs(mv) if mv is not None else 0.0

        s = score_candidate(spread, move_abs, liquidity, volume)
        if s < 3.0:
            continue

        url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com/"
        candidates.append({
            "market_id": market_id,
            "question": question,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "move": mv,
            "liq": liquidity,
            "vol": volume,
            "score": s,
            "url": url,
        })

    candidates.sort(key=lambda x: (x["score"], x["spread"]), reverse=True)
    heartbeat(scanned, len(candidates))

    # alertar top 6 por ciclo (agressivo mas com cooldown por mercado)
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
            "Dica: spread alto costuma dar edge com LIMIT (evitar market)."
        )
        send(msg)

        # news relacionadas (cooldown separado)
        maybe_send_news(c["market_id"], c["question"])

# =========================================================
# 9) MAIN LOOP
# =========================================================
def run():
    send(f"🤖 Bot online ({now_iso()}) | agressivo-controlado | alert≈10min/market | news={'ON' if NEWS_ENABLED else 'OFF'} | tag={BOOT_TAG}")
    while True:
        try:
            scan_once()
        except Exception as e:
            # não derruba
            send(f"⚠️ Erro (sigo rodando): {type(e).__name__}: {str(e)[:200]}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    run()
