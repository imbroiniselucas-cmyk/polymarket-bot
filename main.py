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
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            return

    HTTPServer(("0.0.0.0", port), Handler).serve_forever()

threading.Thread(target=start_health, daemon=True).start()

# ======================================================
# ENV / BOT
# ======================================================
def env(k): return (os.getenv(k) or "").strip()

TELEGRAM_TOKEN   = env("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

print("BOOT_OK: edge-bot running", flush=True)

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    print("❌ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID", flush=True)
    while True:
        time.sleep(60)

bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)

def send(msg: str):
    bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)

# ======================================================
# SETTINGS (tune via Railway Variables)
# ======================================================
def env_int(k, d): 
    try: return int(env(k) or d)
    except: return d

def env_float(k, d):
    try: return float(env(k) or d)
    except: return d

POLL_SECONDS         = env_int("POLL_SECONDS", 45)              # agressivo (scan rápido)
ALERT_COOLDOWN_SEC   = env_int("ALERT_COOLDOWN_SEC", 600)       # 10 min por mercado
HEARTBEAT_SECONDS    = env_int("HEARTBEAT_SECONDS", 600)        # 10 min

MARKET_LIMIT         = env_int("MARKET_LIMIT", 200)

MIN_LIQUIDITY        = env_float("MIN_LIQUIDITY", 20000.0)
MIN_VOLUME           = env_float("MIN_VOLUME", 20000.0)

# Edge filters
MIN_SPREAD           = env_float("MIN_SPREAD", 0.02)            # só spread “real”
MIN_MOVE_ABS         = env_float("MIN_MOVE_ABS", 0.015)         # momentum relevante
MOVE_LOOKBACK_SEC    = env_int("MOVE_LOOKBACK_SEC", 900)        # 15 min

EDGE_SCORE_MIN       = env_float("EDGE_SCORE_MIN", 5.0)         # só edge (corta ruído)
TOP_N_PER_CYCLE      = env_int("TOP_N_PER_CYCLE", 5)            # max alerts por ciclo

# Arbitrage
ARB_ENABLED          = (env("ARB_ENABLED") or "1") == "1"
ARB_MIN_EDGE         = env_float("ARB_MIN_EDGE", 0.01)          # (1 - (askY+askN)) >= 1%
ARB_MAX_ASK          = env_float("ARB_MAX_ASK", 0.98)           # sanity

# News
NEWS_ENABLED         = (env("NEWS_ENABLED") or "1") == "1"
NEWS_MAX_ITEMS       = env_int("NEWS_MAX_ITEMS", 3)
NEWS_COOLDOWN_SEC    = env_int("NEWS_COOLDOWN_SEC", 1800)       # 30 min por mercado
NEWS_QUERY_WORDS     = env_int("NEWS_QUERY_WORDS", 6)

HTTP_TIMEOUT         = env_int("HTTP_TIMEOUT", 15)
MAX_RETRIES          = env_int("MAX_RETRIES", 3)

# ======================================================
# ENDPOINTS
# ======================================================
GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"

# ======================================================
# STATE
# ======================================================
last_alert = {}          # key -> ts
last_heartbeat = 0

price_hist = {}          # token_id -> [(ts, mid)]
news_seen = {}           # market_id -> set(hash)

# ======================================================
# HELPERS
# ======================================================
def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def safe_float(x, default=None):
    try:
        if x is None: return default
        if isinstance(x, (int, float)): return float(x)
        s = str(x).strip()
        return float(s) if s else default
    except:
        return default

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

def http_get_text(url):
    err = None
    for i in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            return r.text
        except Exception as e:
            err = e
            time.sleep(0.6 * i)
    raise err

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

# ======================================================
# POLYMARKET FETCH
# ======================================================
def get_markets():
    return http_get_json(f"{GAMMA}/markets", {
        "active": "true",
        "closed": "false",
        "limit": str(MARKET_LIMIT),
        "offset": "0",
    })

def get_best_prices(token_id: str):
    # sell -> best bid ; buy -> best ask
    bid = ask = None
    try:
        j = http_get_json(f"{CLOB}/price", {"token_id": token_id, "side": "sell"})
        bid = safe_float(j.get("price"), None)
    except:
        pass
    try:
        j = http_get_json(f"{CLOB}/price", {"token_id": token_id, "side": "buy"})
        ask = safe_float(j.get("price"), None)
    except:
        pass
    return bid, ask

# ======================================================
# NEWS (Google News RSS)
# ======================================================
def clean_keywords(question: str):
    q = (question or "").lower()
    q = re.sub(r"[^a-z0-9\s\-\$]", " ", q)
    words = [w for w in q.split() if len(w) >= 4 and w not in {
        "will","what","when","where","which","would","should","could",
        "before","after","today","tomorrow","yesterday","until","through",
        "february","january","march","april","may","june","july","august",
        "september","october","november","december","2024","2025","2026"
    }]
    out, seen = [], set()
    for w in words:
        if w not in seen:
            seen.add(w); out.append(w)
    return out[:NEWS_QUERY_WORDS]

def fetch_news_titles(query: str):
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    xml = http_get_text(url)
    root = ET.fromstring(xml)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link  = item.findtext("link") or ""
        items.append((title, link))
    return items

def maybe_send_news(market_id: str, question: str):
    if not NEWS_ENABLED:
        return

    now = time.time()
    cd_key = f"news::{market_id}"
    if now - last_alert.get(cd_key, 0) < NEWS_COOLDOWN_SEC:
        return

    kws = clean_keywords(question)
    if not kws:
        return
    query = " ".join(kws)

    try:
        items = fetch_news_titles(query)
    except:
        return

    if not items:
        return

    seen = news_seen.get(market_id, set())
    new_items = []
    for (title, link) in items[:12]:
        h = sha1(title + link)
        if h not in seen:
            seen.add(h)
            new_items.append((title, link))
        if len(new_items) >= NEWS_MAX_ITEMS:
            break

    if not new_items:
        news_seen[market_id] = seen
        return

    news_seen[market_id] = seen
    last_alert[cd_key] = now

    lines = [f"🗞️ NEWS (match): {clip(question, 90)}", f"Query: {query}"]
    for (title, link) in new_items:
        lines.append(f"• {clip(title, 110)}")
        if link:
            lines.append(f"  {link}")
    send("\n".join(lines))

# ======================================================
# EDGE SCORING + RECOMMENDATION
# ======================================================
def cooldown_ok(key: str, cooldown: int):
    now = time.time()
    last = last_alert.get(key, 0)
    if now - last >= cooldown:
        last_alert[key] = now
        return True
    return False

def fmt(x):
    return "n/a" if x is None else f"{x:.3f}"

def action_from_sign(move):
    if move is None:
        return "NEUTRO"
    return "BUY YES" if move > 0 else ("BUY NO" if move < 0 else "NEUTRO")

def suggest_limit_prices(bid, ask):
    # sugestões simples e executáveis
    if bid is None or ask is None:
        return None, None
    # “capture” spread: entrar melhor que o ask / sair melhor que o bid
    buy_limit = min(ask - 0.001, bid + 0.001)  # tenta melhorar preço
    sell_limit = max(bid + 0.001, ask - 0.001)
    # bound
    buy_limit = max(0.001, min(0.999, buy_limit))
    sell_limit = max(0.001, min(0.999, sell_limit))
    return buy_limit, sell_limit

def edge_score(spread, move_abs, liq, vol):
    # score agressivo, mas “edge-only”
    s = 0.0
    if spread >= MIN_SPREAD:
        s += min(4.0, spread / MIN_SPREAD * 2.0)
    if move_abs >= MIN_MOVE_ABS:
        s += min(3.0, move_abs / MIN_MOVE_ABS * 1.5)
    s += min(2.0, liq / (MIN_LIQUIDITY * 3.0))
    s += min(2.0, vol / (MIN_VOLUME * 3.0))
    return s

def arb_edge(ask_yes, ask_no):
    # edge = 1 - (askY + askN)
    if ask_yes is None or ask_no is None:
        return None
    if ask_yes <= 0 or ask_no <= 0:
        return None
    if ask_yes > ARB_MAX_ASK or ask_no > ARB_MAX_ASK:
        return None
    return 1.0 - (ask_yes + ask_no)

# ======================================================
# SCAN LOOP
# ======================================================
def heartbeat(scanned, candidates, alerts_sent):
    global last_heartbeat
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_SECONDS:
        last_heartbeat = now
        send(f"✅ BOT VIVO ({now_iso()}) | scanned={scanned} | candidates={candidates} | sent={alerts_sent}")

def scan_once():
    markets = get_markets()
    ts = time.time()

    scanned = 0
    candidates = []

    for m in markets:
        scanned += 1
        market_id = str(m.get("id") or "")
        question = (m.get("question") or "").strip()
        slug = (m.get("slug") or "").strip()

        liq = safe_float(m.get("liquidity"), 0.0) or 0.0
        vol = safe_float(m.get("volume"), safe_float(m.get("volume24hr"), 0.0)) or 0.0

        if not market_id or not question or not slug:
            continue
        if liq < MIN_LIQUIDITY or vol < MIN_VOLUME:
            continue

        clob_ids = m.get("clobTokenIds") or []
        if not isinstance(clob_ids, list) or len(clob_ids) < 1:
            continue

        # token0 proxy (geralmente YES). token1 (se existir) proxy NO.
        token_yes = str(clob_ids[0])
        token_no  = str(clob_ids[1]) if len(clob_ids) >= 2 else None

        bid_y, ask_y = get_best_prices(token_yes)
        if bid_y is None or ask_y is None:
            continue

        mid_y = (bid_y + ask_y) / 2.0
        record_mid(token_yes, ts, mid_y)

        spread_y = ask_y - bid_y
        mv = move_over_lookback(token_yes, ts, MOVE_LOOKBACK_SEC)
        mv_abs = abs(mv) if mv is not None else 0.0

        # base edge score (YES-side microstructure + momentum + depth proxies)
        score = edge_score(spread_y, mv_abs, liq, vol)

        # optional arb check
        arb = None
        if ARB_ENABLED and token_no:
            bid_n, ask_n = get_best_prices(token_no)
            if bid_n is not None and ask_n is not None:
                arb = arb_edge(ask_y, ask_n)

        url = f"https://polymarket.com/market/{slug}"

        # keep only edge-only candidates
        if score >= EDGE_SCORE_MIN or (arb is not None and arb >= ARB_MIN_EDGE):
            candidates.append({
                "market_id": market_id,
                "question": question,
                "url": url,
                "liq": liq,
                "vol": vol,
                "bid_y": bid_y, "ask_y": ask_y,
                "spread_y": spread_y,
                "mv": mv, "mv_abs": mv_abs,
                "score": score,
                "arb": arb,
                "has_no": token_no is not None
            })

    # rank: arb first, then score
    def rank_key(c):
        arb_bonus = (c["arb"] if c["arb"] is not None else -999)
        return (arb_bonus, c["score"], c["spread_y"], c["mv_abs"])

    candidates.sort(key=rank_key, reverse=True)

    alerts_sent = 0

    for c in candidates[:TOP_N_PER_CYCLE]:
        # cooldown por mercado
        if not cooldown_ok(f"m::{c['market_id']}", ALERT_COOLDOWN_SEC):
            continue

        # clear recommendation
        action = action_from_sign(c["mv"])
        buy_limit, sell_limit = suggest_limit_prices(c["bid_y"], c["ask_y"])

        # arb recommendation (if strong)
        arb_line = ""
        if c["arb"] is not None and c["arb"] >= ARB_MIN_EDGE and c["has_no"]:
            arb_line = (
                f"\n🧮 ARB DETECTADO: ask(YES)+ask(NO)={c['ask_y']:.3f}+… < 1  "
                f"(edge≈{c['arb']*100:.2f}%)\n"
                "AÇÃO CLARA: **BUY BOTH (YES + NO) via LIMIT** para travar payout=1."
            )

        # spread edge message
        edge_line = ""
        if c["spread_y"] >= MIN_SPREAD:
            edge_line = (
                f"\n📌 EDGE (spread): {c['spread_y']:.3f} (alto) → prefira **LIMIT**, evita slippage."
            )

        momentum_line = ""
        if c["mv"] is not None and c["mv_abs"] >= MIN_MOVE_ABS:
            momentum_line = f"\n⚡ EDGE (momentum {MOVE_LOOKBACK_SEC//60}m): Δ≈{c['mv']:.3f} → {action}"

        msg = (
            "🚨 EDGE ALERT (somente edge)\n"
            f"🧠 {clip(c['question'], 160)}\n"
            f"Score: {c['score']:.2f} | Liq: {c['liq']:.0f} | Vol: {c['vol']:.0f}\n"
            f"YES Bid: {fmt(c['bid_y'])} | YES Ask: {fmt(c['ask_y'])} | Spread: {c['spread_y']:.3f}\n"
            f"🔗 {c['url']}\n"
            f"{edge_line}{momentum_line}{arb_line}\n"
            "\n✅ RECOMENDAÇÃO (executável):\n"
            f"- AÇÃO: {action}\n"
            f"- BUY LIMIT sugerido (YES): {buy_limit:.3f}\n"
            f"- SELL LIMIT sugerido (YES): {sell_limit:.3f}\n"
            "\nObs: confirme no orderbook se Token0=YES/Token1=NO neste mercado."
        )
        send(msg)
        alerts_sent += 1

        # news: só pra mercados com edge forte
        if NEWS_ENABLED and (c["score"] >= EDGE_SCORE_MIN or (c["arb"] is not None and c["arb"] >= ARB_MIN_EDGE)):
            maybe_send_news(c["market_id"], c["question"])

    heartbeat(scanned, len(candidates), alerts_sent)

def run():
    send(
        "🤖 Bot ONLINE (EDGE agressivo)\n"
        f"- scan: {POLL_SECONDS}s\n"
        f"- cooldown por mercado: {ALERT_COOLDOWN_SEC//60} min\n"
        f"- edge-only score>= {EDGE_SCORE_MIN}\n"
        f"- arb>= {ARB_MIN_EDGE*100:.1f}% | news={'ON' if NEWS_ENABLED else 'OFF'}\n"
        "✅ Dica: sempre usar LIMIT quando o alert citar spread/arb."
    )

    while True:
        try:
            scan_once()
        except Exception as e:
            # não derruba o bot
            send(f"⚠️ Erro (sigo rodando): {type(e).__name__}: {str(e)[:180]}")
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    run()
