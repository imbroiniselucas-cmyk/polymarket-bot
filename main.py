#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import re
import hashlib
from datetime import datetime, timezone
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
import xml.etree.ElementTree as ET

# ======================================================
# ENV (não muda suas variables)
# ======================================================

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()
CLOB_BASE = os.getenv("CLOB_BASE", "https://clob.polymarket.com").strip()

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "600"))  # 10min

MAX_SPREAD_CENTS = float(os.getenv("MAX_SPREAD_CENTS", "2.0"))  # 2¢
MIN_TOP_USD = float(os.getenv("MIN_TOP_USD", "150"))            # $ no topo do book (aprox)
MIN_MID_LIQ_USD = float(os.getenv("MIN_MID_LIQ_USD", "500"))     # se market tiver liquidity

MIN_SCORE = float(os.getenv("MIN_SCORE", "35"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "6"))

NEWS_LOOKBACK_HOURS = int(os.getenv("NEWS_LOOKBACK_HOURS", "72"))
NEWS_MIN_MATCH = float(os.getenv("NEWS_MIN_MATCH", "0.18"))

USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; PolymarketBot/1.1)")
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12"))

STATE_FILE = os.getenv("STATE_FILE", "/tmp/bot_state.json")

NEWS_SOURCE_WHITELIST = {
    "reuters", "ap", "associated press", "bbc", "cnn", "espn", "the athletic",
    "nytimes", "new york times", "washington post", "wsj", "wall street journal",
    "guardian", "sky sports", "nfl", "nbc", "cbs", "fox"
}

NEGATION_PHRASES = [
    "won't attend", "will not attend", "not attending", "ruled out", "will miss",
    "won't be there", "will not be there", "not expected to attend", "unlikely to attend",
    "confirmed he won't", "confirmed she won't", "won't appear", "will not appear",
    "will skip", "skipping", "not going to", "won't go", "will not go"
]

# ======================================================
# UTILS
# ======================================================

def log(msg):
    print(msg, flush=True)

def now_utc():
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

def http_get(url: str, headers=None, timeout=TIMEOUT):
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    req = Request(url, headers=hdrs, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            # tenta utf-8, fallback latin-1
            try:
                return resp.status, body.decode("utf-8", errors="replace")
            except Exception:
                return resp.status, body.decode("latin-1", errors="replace")
    except HTTPError as e:
        return e.code, ""
    except URLError:
        return 0, ""
    except Exception:
        return 0, ""

def http_post_json(url: str, payload: dict, headers=None, timeout=TIMEOUT):
    hdrs = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers=hdrs, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            try:
                txt = body.decode("utf-8", errors="replace")
            except Exception:
                txt = body.decode("latin-1", errors="replace")
            return resp.status, txt
    except HTTPError as e:
        return e.code, ""
    except URLError:
        return 0, ""
    except Exception:
        return 0, ""

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

# ======================================================
# TELEGRAM
# ======================================================

def tg_send(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    status, _ = http_post_json(url, payload)
    if status != 200:
        log(f"⚠️ Telegram send failed: {status}")
        return False
    return True

# ======================================================
# MARKETS FETCH
# ======================================================

def fetch_markets_from_endpoint():
    if not POLY_ENDPOINT:
        return []
    status, txt = http_get(POLY_ENDPOINT)
    if status != 200 or not txt:
        log(f"❌ POLY_ENDPOINT fetch error: status={status}")
        return []
    try:
        data = json.loads(txt)
        if isinstance(data, dict) and "markets" in data:
            data = data["markets"]
        return data if isinstance(data, list) else []
    except Exception as e:
        log(f"❌ JSON parse error: {e}")
        return []

# ======================================================
# CLOB ORDERBOOK (spread real)
# ======================================================

def clob_get_book(token_id: str):
    if not token_id:
        return None
    urls = [
        f"{CLOB_BASE}/book?token_id={token_id}",
        f"{CLOB_BASE}/orderbook?token_id={token_id}",
    ]
    for url in urls:
        status, txt = http_get(url)
        if status != 200 or not txt:
            continue
        try:
            return json.loads(txt)
        except Exception:
            continue
    return None

def best_bid_ask_from_book(book_json):
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

def spread_cents(bidask):
    if not bidask:
        return None
    bid_p = bidask["bid"][0]
    ask_p = bidask["ask"][0]
    if bid_p is None or ask_p is None:
        return None
    return max(0.0, (ask_p - bid_p) * 100.0)

def top_usd_estimate(bidask, mid_price):
    if not bidask or mid_price is None:
        return 0.0
    bid_p, bid_s = bidask["bid"]
    ask_p, ask_s = bidask["ask"]
    top_size = min(bid_s or 0.0, ask_s or 0.0)
    return float(top_size) * float(mid_price)

# ======================================================
# NEWS (Google News RSS)
# ======================================================

def tokenize(text: str):
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    toks = [t for t in text.split() if len(t) >= 3]
    stop = {"will","the","and","for","with","from","that","this","have","has","about","over","under","into","after","before"}
    return [t for t in toks if t not in stop]

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
    status, txt = http_get(url)
    if status != 200 or not txt:
        return []
    try:
        root = ET.fromstring(txt)
    except Exception:
        return []

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

def news_score_for_market(question: str):
    q_tokens = tokenize(question)
    if not q_tokens:
        return 0.0, None, False

    query = " ".join(q_tokens[:10])
    items = google_news_rss(query)

    lookback_ts = now_utc().timestamp() - NEWS_LOOKBACK_HOURS * 3600
    best_score = 0.0
    best_item = None
    best_neg = False

    for it in items:
        title_l = (it["title"] or "").lower()
        it_tokens = tokenize(it["title"])
        sim = jaccard(q_tokens, it_tokens)

        if sim < NEWS_MIN_MATCH:
            continue

        rec = 0.0
        if it["published"] is not None:
            ts = it["published"].timestamp()
            if ts >= lookback_ts:
                hours_ago = max(0.0, (now_utc().timestamp() - ts) / 3600.0)
                rec = math.exp(-hours_ago / 24.0)

        src = (it.get("source") or "").lower()
        src_boost = 0.15 if any(w in src for w in NEWS_SOURCE_WHITELIST) else 0.0

        neg = any(p in title_l for p in NEGATION_PHRASES)

        raw = (0.65 * sim) + (0.35 * rec) + src_boost
        if neg:
            raw *= 0.45  # penaliza

        score = clamp(raw * 100.0, 0.0, 100.0)
        if score > best_score:
            best_score = score
            best_item = it
            best_neg = neg

    return best_score, best_item, best_neg

# ======================================================
# MARKET PARSING
# ======================================================

def get_prices(m):
    yes = safe_float(m.get("yes_price"))
    no = safe_float(m.get("no_price"))

    if yes is None and isinstance(m.get("prices"), dict):
        yes = safe_float(m["prices"].get("yes"))
        no = safe_float(m["prices"].get("no"))

    if yes is not None and no is None:
        no = 1.0 - yes
    if no is not None and yes is None:
        yes = 1.0 - no

    if yes is None or no is None:
        return None, None
    return clamp(yes, 0.0, 1.0), clamp(no, 0.0, 1.0)

def extract_token_ids(m):
    yes_tid = m.get("yes_token_id") or m.get("token_id_yes") or m.get("yesTokenId")
    no_tid = m.get("no_token_id") or m.get("token_id_no") or m.get("noTokenId")
    return (str(yes_tid) if yes_tid else None), (str(no_tid) if no_tid else None)

def market_liq_usd(m):
    return safe_float(m.get("liquidity") or m.get("liq") or m.get("liquidity_usd") or m.get("liquidityUSD"), 0.0)

def market_url(m):
    return m.get("url") or m.get("link") or m.get("market_url") or ""

def market_question(m):
    return m.get("question") or m.get("title") or m.get("name") or ""

# ======================================================
# RECOMMENDATION
# ======================================================

def compute_entry_reco(m, yes_price, no_price, yes_bidask, no_bidask, news_info):
    liq = market_liq_usd(m)
    if liq and liq < MIN_MID_LIQ_USD:
        return None

    news_score, news_item, news_neg = news_info

    spread_yes = spread_cents(yes_bidask) if yes_bidask else None
    spread_no = spread_cents(no_bidask) if no_bidask else None

    # precisa book real pra proteger de spread trap
    candidates = []
    if spread_yes is not None:
        mid = yes_price
        top_usd = top_usd_estimate(yes_bidask, mid)
        candidates.append(("YES", spread_yes, top_usd, yes_price))
    if spread_no is not None:
        mid = no_price
        top_usd = top_usd_estimate(no_bidask, mid)
        candidates.append(("NO", spread_no, top_usd, no_price))

    if not candidates:
        return None

    # filtros duros anti-trap
    good = []
    for side, spr, top_usd, px in candidates:
        if spr > MAX_SPREAD_CENTS:
            continue
        if top_usd < MIN_TOP_USD:
            continue
        good.append((side, spr, top_usd, px))

    if not good:
        return None

    # escolhe: menor spread; em empate, mais barato
    good.sort(key=lambda x: (x[1], x[3]))
    side, spr, top_usd, px = good[0]

    spread_component = clamp((MAX_SPREAD_CENTS - spr) / MAX_SPREAD_CENTS, 0.0, 1.0)
    depth_component = clamp(math.log1p(top_usd) / math.log1p(1000.0), 0.0, 1.0)
    price_component = clamp((0.60 - px) / 0.60, 0.0, 1.0)

    news_component = clamp(news_score / 100.0, 0.0, 1.0)
    if news_neg:
        news_component *= 0.55

    raw = (
        0.40 * spread_component +
        0.30 * depth_component +
        0.10 * price_component +
        0.20 * news_component
    ) * 100.0

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
        "question": market_question(m),
        "url": market_url(m),
        "liq": liq
    }

# ======================================================
# MAIN
# ======================================================

def main():
    st = load_state()
    tg_send("✅ BOT ON: stdlib-only (no requests) | anti-spread-trap | smarter news")

    while True:
        try:
            markets = fetch_markets_from_endpoint()
            if not markets:
                log("⚠️ No markets fetched (POLY_ENDPOINT empty or failed).")
                time.sleep(POLL_SECONDS)
                continue

            scored = []
            for m in markets:
                q = market_question(m)
                if not q:
                    continue

                yes_price, no_price = get_prices(m)
                if yes_price is None:
                    continue

                yes_tid, no_tid = extract_token_ids(m)

                # orderbooks
                yes_bidask = None
                no_bidask = None
                if yes_tid:
                    ybook = clob_get_book(yes_tid)
                    yes_bidask = best_bid_ask_from_book(ybook) if ybook else None
                if no_tid:
                    nbook = clob_get_book(no_tid)
                    no_bidask = best_bid_ask_from_book(nbook) if nbook else None

                # news
                news_info = news_score_for_market(q)

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

                # cooldown 3h
                if time.time() - last_ts < 3 * 3600:
                    continue

                news_line = "News: none"
                if r["news_item"]:
                    src = (r["news_item"].get("source") or "").strip()
                    ttl = (r["news_item"].get("title") or "").strip()
                    news_line = f"News({int(r['news_score'])}): {src} — {ttl}"

                neg_flag = " ⚠️(negation-hit)" if r["news_neg"] else ""

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
