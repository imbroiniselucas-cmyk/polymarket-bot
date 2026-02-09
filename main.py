#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import re
import hashlib
import requests
from datetime import datetime, timezone
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

# ======================================================
# CONFIG (mantém TELEGRAM_TOKEN/CHAT_ID iguais)
# ======================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Seu endpoint de markets (o mesmo que você já usava), opcional
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()

# CLOB (orderbook) - Polymarket
CLOB_BASE = os.getenv("CLOB_BASE", "https://clob.polymarket.com").strip()

# Frequência
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "600"))  # 10 min default

# Filtros anti-spread-trap
MAX_SPREAD_CENTS = float(os.getenv("MAX_SPREAD_CENTS", "2.0"))  # 2¢
MIN_TOP_USD = float(os.getenv("MIN_TOP_USD", "150"))            # $ no topo do book (aprox)
MIN_MID_LIQ_USD = float(os.getenv("MIN_MID_LIQ_USD", "500"))     # liquidez mínima do mercado (se disponível)

# Score / alertas
MIN_SCORE = float(os.getenv("MIN_SCORE", "35"))  # seu padrão de “só alertas bons”
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "6"))

# Notícias
NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "72"))
NEWS_MIN_MATCH = float(os.getenv("NEWS_MIN_MATCH", "0.18"))  # threshold de relevância
NEWS_SOURCE_WHITELIST = {
    "reuters", "ap", "associated press", "bbc", "cnn", "espn", "the athletic",
    "nytimes", "new york times", "washington post", "wsj", "wall street journal",
    "guardian", "sky sports", "nfl", "nbc", "cbs", "fox", "bleacher report"
}

NEGATION_PHRASES = [
    "won't attend", "will not attend", "not attending", "ruled out", "will miss",
    "won't be there", "will not be there", "not expected to attend", "unlikely to attend",
    "confirmed he won't", "confirmed she won't", "won't appear", "will not appear",
    "will skip", "skipping", "out of super bowl", "not going to the super bowl"
]

USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; PolymarketBot/1.0)")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))

STATE_FILE = os.getenv("STATE_FILE", "/tmp/bot_state.json")

# ======================================================
# HELPERS
# ======================================================

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def safe_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default

def sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"seen": {}}

def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def log(msg):
    print(msg, flush=True)

# ======================================================
# TELEGRAM
# ======================================================

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        ok = r.status_code == 200
        if not ok:
            log(f"⚠️ Telegram send failed: {r.status_code} {r.text[:200]}")
        return ok
    except Exception as e:
        log(f"⚠️ Telegram exception: {e}")
        return False

# ======================================================
# MARKETS FETCH
# ======================================================

def fetch_markets_from_endpoint():
    if not POLY_ENDPOINT:
        return []
    try:
        r = requests.get(POLY_ENDPOINT, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
        # Aceita lista ou dict com "markets"
        if isinstance(data, dict) and "markets" in data:
            data = data["markets"]
        if not isinstance(data, list):
            return []
        return data
    except Exception as e:
        log(f"❌ POLY_ENDPOINT fetch error: {e}")
        return []

# ======================================================
# CLOB ORDERBOOK (spread real)
# ======================================================

def clob_get_book(token_id: str):
    """
    Polymarket CLOB geralmente expõe endpoints de orderbook.
    Aqui tentamos /book?token_id=... (ou /orderbook).
    Ajuste se o seu stack já usa outro path.
    """
    if not token_id:
        return None

    paths = [
        f"{CLOB_BASE}/book?token_id={token_id}",
        f"{CLOB_BASE}/orderbook?token_id={token_id}",
    ]
    for url in paths:
        try:
            r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
            if r.status_code != 200:
                continue
            return r.json()
        except Exception:
            continue
    return None

def best_bid_ask_from_book(book_json):
    """
    Espera algo como:
    { "bids":[{"price":"0.52","size":"123"}...], "asks":[...] }
    ou listas [ [price, size], ... ]
    """
    if not book_json:
        return None

    bids = book_json.get("bids") or book_json.get("buy") or []
    asks = book_json.get("asks") or book_json.get("sell") or []

    def parse_level(lv):
        if isinstance(lv, dict):
            p = safe_float(lv.get("price"))
            s = safe_float(lv.get("size") or lv.get("amount") or lv.get("quantity"))
            return p, s
        if isinstance(lv, (list, tuple)) and len(lv) >= 2:
            return safe_float(lv[0]), safe_float(lv[1])
        return None, None

    best_bid = None
    best_ask = None

    for lv in bids:
        p, s = parse_level(lv)
        if p is None:
            continue
        if best_bid is None or p > best_bid[0]:
            best_bid = (p, s or 0.0)

    for lv in asks:
        p, s = parse_level(lv)
        if p is None:
            continue
        if best_ask is None or p < best_ask[0]:
            best_ask = (p, s or 0.0)

    if best_bid is None or best_ask is None:
        return None
    return {"bid": best_bid, "ask": best_ask}

def spread_cents_from_bidask(bidask):
    if not bidask:
        return None
    bid_p = bidask["bid"][0]
    ask_p = bidask["ask"][0]
    if bid_p is None or ask_p is None:
        return None
    return max(0.0, (ask_p - bid_p) * 100.0)

def top_usd_estimate(bidask, mid_price=None):
    """
    Estima $ no topo (bem aproximado):
    size * price (em $1 payout terms). Size geralmente é em shares.
    """
    if not bidask:
        return 0.0
    bid_p, bid_s = bidask["bid"]
    ask_p, ask_s = bidask["ask"]
    p = mid_price if mid_price is not None else (bid_p + ask_p) / 2.0
    # usa o menor dos dois lados pra garantir saída mínima
    top_size = min(bid_s or 0.0, ask_s or 0.0)
    return float(top_size) * float(p)

# ======================================================
# NEWS (Google News RSS - sem API key)
# ======================================================

def tokenize(text: str):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    toks = [t for t in text.split() if len(t) >= 3]
    # remove palavras muito comuns
    stop = {"will","the","and","for","with","from","that","this","have","has","about","over","under","into","after","before"}
    toks = [t for t in toks if t not in stop]
    return toks

def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

def parse_rss_datetime(s):
    # Ex: "Mon, 08 Feb 2026 21:10:00 GMT"
    try:
        return datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def google_news_rss(query: str, max_items=12):
    q = quote_plus(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            dt = parse_rss_datetime(pub) if pub else None
            source = ""
            src_el = item.find("source")
            if src_el is not None and src_el.text:
                source = src_el.text.strip()
            items.append({"title": title, "link": link, "published": dt, "source": source})
        return items
    except Exception:
        return []

def news_score_for_market(question: str):
    """
    Retorna:
      - best_score (0-100)
      - best_item (dict)
      - negation_flag (bool)
    """
    q_tokens = tokenize(question)
    if not q_tokens:
        return 0.0, None, False

    # Query mais curta e “clean”
    query = " ".join(q_tokens[:10])
    items = google_news_rss(query)

    lookback = now_utc().timestamp() - NEWS_LOOKBACK_HOURS * 3600
    best = (0.0, None, False)

    for it in items:
        text = (it["title"] or "").lower()
        it_tokens = tokenize(it["title"])
        sim = jaccard(q_tokens, it_tokens)

        # recência
        rec = 0.0
        if it["published"] is not None:
            ts = it["published"].timestamp()
            if ts >= lookback:
                # 0..1 (mais recente -> maior)
                hours_ago = max(0.0, (now_utc().timestamp() - ts) / 3600.0)
                rec = math.exp(-hours_ago / 24.0)  # cai com ~24h
        # fonte
        src = (it.get("source") or "").lower()
        src_boost = 0.15 if any(w in src for w in NEWS_SOURCE_WHITELIST) else 0.0

        # negação (pra evitar “vai estar” quando notícias dizem que “não vai”)
        neg = any(p in text for p in NEGATION_PHRASES)

        # score base
        raw = (0.65 * sim) + (0.35 * rec) + src_boost
        if neg:
            raw *= 0.45  # penaliza forte (notícia contradiz presença)

        # threshold mínimo de match
        if sim < NEWS_MIN_MATCH:
            continue

        score100 = clamp(raw * 100.0, 0.0, 100.0)
        if score100 > best[0]:
            best = (score100, it, neg)

    return best

# ======================================================
# SCORING & FILTERS
# ======================================================

def get_prices(m):
    """
    Tenta extrair YES/NO price de vários formatos comuns.
    Retorna (yes, no) em 0..1 ou (None, None)
    """
    yes = safe_float(m.get("yes_price"))
    no = safe_float(m.get("no_price"))

    # formatos alternativos
    if yes is None and "prices" in m and isinstance(m["prices"], dict):
        yes = safe_float(m["prices"].get("yes"))
        no = safe_float(m["prices"].get("no"))

    # Se só tiver uma, deriva a outra
    if yes is not None and no is None:
        no = 1.0 - yes
    if no is not None and yes is None:
        yes = 1.0 - no

    if yes is None or no is None:
        return None, None
    return clamp(yes, 0.0, 1.0), clamp(no, 0.0, 1.0)

def extract_token_ids(m):
    yes_tid = m.get("yes_token_id") or m.get("token_id_yes") or m.get("yesTokenId")
    no_tid  = m.get("no_token_id")  or m.get("token_id_no")  or m.get("noTokenId")
    return (str(yes_tid) if yes_tid else None), (str(no_tid) if no_tid else None)

def market_liq_usd(m):
    # best-effort
    return safe_float(m.get("liquidity") or m.get("liq") or m.get("liquidity_usd") or m.get("liquidityUSD"), 0.0)

def market_url(m):
    return m.get("url") or m.get("link") or m.get("market_url") or ""

def market_question(m):
    return m.get("question") or m.get("title") or m.get("name") or ""

def compute_entry_reco(m, yes_price, no_price, yes_bidask, no_bidask, news_info):
    """
    Decide se recomenda BUY (YES ou NO), e calcula score.
    Foco: evitar spread trap e usar news melhor.
    """
    question = market_question(m)

    # Checagens de liquidez básica
    liq = market_liq_usd(m)
    if liq and liq < MIN_MID_LIQ_USD:
        return None

    # Spread real via book
    spread_yes = spread_cents_from_bidask(yes_bidask) if yes_bidask else None
    spread_no  = spread_cents_from_bidask(no_bidask)  if no_bidask  else None

    # Se não conseguimos book, reduz a agressividade (pra não te prender)
    has_books = (spread_yes is not None) or (spread_no is not None)

    # News score
    news_score, news_item, news_neg = news_info

    # Heurística simples: preferir lado com menor spread e preço “mais barato” (mais convexidade)
    candidates = []
    if spread_yes is not None:
        top_usd = top_usd_estimate(yes_bidask, mid_price=yes_price)
        candidates.append(("YES", spread_yes, top_usd, yes_price))
    if spread_no is not None:
        top_usd = top_usd_estimate(no_bidask, mid_price=no_price)
        candidates.append(("NO", spread_no, top_usd, no_price))

    if not candidates:
        if not has_books:
            return None
        return None

    # filtra por spread e topo
    candidates2 = []
    for side, spr, top_usd, px in candidates:
        if spr is None:
            continue
        if spr > MAX_SPREAD_CENTS:
            continue
        if top_usd < MIN_TOP_USD:
            continue
        candidates2.append((side, spr, top_usd, px))

    if not candidates2:
        return None

    # escolhe melhor: menor spread, e preço mais “barato” (mais edge em movimentos)
    candidates2.sort(key=lambda x: (x[1], x[3]))
    side, spr, top_usd, px = candidates2[0]

    # Score: combina tight spread + topo + notícia
    # (quanto menor spread e maior topo, melhor)
    spread_component = clamp((MAX_SPREAD_CENTS - spr) / MAX_SPREAD_CENTS, 0.0, 1.0)  # 0..1
    depth_component = clamp(math.log1p(top_usd) / math.log1p(1000.0), 0.0, 1.0)      # 0..1
    price_component = clamp((0.60 - px) / 0.60, 0.0, 1.0)                             # favorece “barato” <= 0.60

    # notícia ajuda, mas não manda sozinha
    news_component = clamp(news_score / 100.0, 0.0, 1.0)
    if news_neg:
        news_component *= 0.55

    # Se não tiver book, derruba score
    book_penalty = 0.75 if not has_books else 1.0

    raw = (
        0.38 * spread_component +
        0.30 * depth_component +
        0.12 * price_component +
        0.20 * news_component
    ) * 100.0 * book_penalty

    score = clamp(raw, 0.0, 100.0)

    return {
        "side": side,
        "score": score,
        "spread_cents": spr,
        "top_usd": top_usd,
        "price": px,
        "news_score": news_score,
        "news_item": news_item,
        "news_neg": news_neg,
        "question": question,
        "url": market_url(m),
        "liq": liq
    }

# ======================================================
# MAIN LOOP
# ======================================================

def main():
    st = load_state()
    tg_send("✅ BOT ON: anti-spread-trap + news relevance enabled")

    while True:
        try:
            markets = fetch_markets_from_endpoint()
            if not markets:
                log("⚠️ No markets from POLY_ENDPOINT (empty).")
                time.sleep(POLL_SECONDS)
                continue

            scored = []
            for m in markets:
                question = market_question(m)
                if not question:
                    continue

                yes_price, no_price = get_prices(m)
                if yes_price is None:
                    continue

                yes_tid, no_tid = extract_token_ids(m)

                yes_bidask = None
                no_bidask = None

                if yes_tid:
                    ybook = clob_get_book(yes_tid)
                    yes_bidask = best_bid_ask_from_book(ybook) if ybook else None
                if no_tid:
                    nbook = clob_get_book(no_tid)
                    no_bidask = best_bid_ask_from_book(nbook) if nbook else None

                # notícias
                news_info = news_score_for_market(question)

                reco = compute_entry_reco(m, yes_price, no_price, yes_bidask, no_bidask, news_info)
                if not reco:
                    continue

                if reco["score"] < MIN_SCORE:
                    continue

                scored.append(reco)

            scored.sort(key=lambda x: x["score"], reverse=True)
            to_send = scored[:MAX_ALERTS_PER_CYCLE]

            sent = 0
            for r in to_send:
                key = sha1(f"{r['url']}|{r['side']}|{round(r['price'],4)}|{round(r['spread_cents'],2)}")
                last_ts = st["seen"].get(key, 0)
                # evita repetir muito: 3h
                if time.time() - last_ts < 3 * 3600:
                    continue

                news_line = "News: none"
                if r["news_item"]:
                    src = (r["news_item"].get("source") or "").strip()
                    ttl = (r["news_item"].get("title") or "").strip()
                    news_line = f"News({int(r['news_score'])}): {src} — {ttl}"

                neg_flag = " ⚠️(neg-phrases)" if r["news_neg"] else ""

                msg = (
                    f"🚨 BUY SIGNAL ({r['side']})\n"
                    f"🧠 Score: {r['score']:.1f}\n"
                    f"💰 Price: {r['price']:.3f}\n"
                    f"📉 Spread: {r['spread_cents']:.2f}¢ (max {MAX_SPREAD_CENTS:.2f}¢)\n"
                    f"🏦 TopDepth≈ ${r['top_usd']:.0f} (min ${MIN_TOP_USD:.0f})\n"
                    f"💧 Liq≈ ${r['liq']:.0f}\n"
                    f"📰 {news_line}{neg_flag}\n"
                    f"{r['url']}"
                )

                if tg_send(msg):
                    st["seen"][key] = time.time()
                    sent += 1

            save_state(st)
            log(f"Cycle done. sent={sent} candidates={len(scored)}")
        except Exception as e:
            log(f"❌ loop error: {e}")

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
