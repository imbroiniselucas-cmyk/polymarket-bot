#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket ACTION-only bot with external signals (news/weather/crypto)

What it does now:
- ACTION-only alerts (no WATCH, no status spam)
- Requires "edge": market implied probability vs external estimated probability
- Confirmation: requires condition in 2 consecutive scans
- Less aggressive: cooldowns + hourly cap + per-market cooldown

Env vars:
- TELEGRAM_TOKEN
- TELEGRAM_CHAT_ID

Optional tuning:
- SCAN_SECONDS (default 90)
- MIN_LIQ (default 50000)
- EDGE_MIN_PCT (default 10)         # min divergence in probability points
- CONFIRM_SCANS (default 2)         # consecutive scans to confirm before ACTION
- PER_MARKET_COOLDOWN_SEC (default 2700) # 45 min
- GLOBAL_COOLDOWN_SEC (default 240)      # 4 min
- MAX_ALERTS_PER_HOUR (default 6)
- MAX_ALERTS_PER_CYCLE (default 2)

Notes:
- News: uses RSS from Google News (no key). Heuristic scoring.
- Weather: uses Open-Meteo (no key). City-level forecast (approx).
- Crypto: uses CoinGecko (no key) + simple volatility estimate.

This is designed to be "more opportunistic" than "momentum chasing".
"""

import os
import time
import json
import math
import re
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

# ----------------------------
# CONFIG
# ----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "90"))

MIN_LIQ = float(os.getenv("MIN_LIQ", "50000"))
EDGE_MIN_PCT = float(os.getenv("EDGE_MIN_PCT", "10"))  # probability points (e.g., 10 = 10%)
CONFIRM_SCANS = int(os.getenv("CONFIRM_SCANS", "2"))

PER_MARKET_COOLDOWN_SEC = int(os.getenv("PER_MARKET_COOLDOWN_SEC", "2700"))  # 45 min
GLOBAL_COOLDOWN_SEC = int(os.getenv("GLOBAL_COOLDOWN_SEC", "240"))           # 4 min
MAX_ALERTS_PER_HOUR = int(os.getenv("MAX_ALERTS_PER_HOUR", "6"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "2"))

STATE_PATH = os.getenv("STATE_PATH", "/tmp/poly_state.json")

UA = "poly-edge-bot/2.0"

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"


# ----------------------------
# TELEGRAM
# ----------------------------
def tg_send(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            timeout=20,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "disable_web_page_preview": True,
            },
        )
    except Exception as e:
        print("Telegram send error:", e)


# ----------------------------
# STATE
# ----------------------------
def load_state() -> Dict[str, Any]:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "last_alert_ts": {},          # market_id -> epoch
            "global_last_alert_ts": 0,
            "hour_bucket": {},            # hour_key -> count
            "confirm": {},                # market_id -> consecutive hits
            "cache": {                    # small caches for APIs
                "news": {},
                "crypto": {},
                "weather": {},
            },
        }


def save_state(state: Dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def hour_key(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(ts))


def global_hourly_cap_ok(state: Dict[str, Any], ts: float) -> bool:
    key = hour_key(ts)
    count = int(state.get("hour_bucket", {}).get(key, 0))
    return count < MAX_ALERTS_PER_HOUR


def bump_hourly_count(state: Dict[str, Any], ts: float) -> None:
    key = hour_key(ts)
    state.setdefault("hour_bucket", {})
    state["hour_bucket"][key] = int(state["hour_bucket"].get(key, 0)) + 1


def cooldown_ok(state: Dict[str, Any], market_id: str, ts: float) -> bool:
    last_m = float(state.get("last_alert_ts", {}).get(market_id, 0))
    if ts - last_m < PER_MARKET_COOLDOWN_SEC:
        return False
    last_g = float(state.get("global_last_alert_ts", 0))
    if ts - last_g < GLOBAL_COOLDOWN_SEC:
        return False
    return True


def mark_alert(state: Dict[str, Any], market_id: str, ts: float) -> None:
    state.setdefault("last_alert_ts", {})
    state["last_alert_ts"][market_id] = ts
    state["global_last_alert_ts"] = ts
    bump_hourly_count(state, ts)
    # reset confirm counter after alert
    state.setdefault("confirm", {})
    state["confirm"][market_id] = 0


# ----------------------------
# HELPERS
# ----------------------------
def _to_float(x: Any) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float)):
            return float(x)
        return float(str(x).strip().replace(",", ""))
    except Exception:
        return 0.0


def clean_title(s: str, n: int = 115) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip())
    return s if len(s) <= n else s[: n - 1] + "…"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_dt_iso(s: str) -> Optional[dt.datetime]:
    try:
        if not s:
            return None
        # Gamma commonly returns ISO strings
        # Examples: "2026-02-28T23:59:59Z" or with offset
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


# ----------------------------
# POLYMARKET FETCH
# ----------------------------
def fetch_markets(limit: int = 200) -> List[Dict[str, Any]]:
    r = requests.get(
        GAMMA_MARKETS_URL,
        params={
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": 0,
            "order": "volume24hr",
            "ascending": "false",
        },
        timeout=25,
        headers={"User-Agent": UA},
    )
    r.raise_for_status()
    raw = r.json()
    return normalize_markets(raw)


def normalize_markets(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    raw_list = raw if isinstance(raw, list) else []
    for m in raw_list:
        try:
            mid = str(m.get("id") or "")
            if not mid:
                continue

            question = (m.get("question") or m.get("title") or "").strip()
            slug = (m.get("slug") or "").strip()
            url = m.get("url") or (f"https://polymarket.com/market/{slug}" if slug else "")

            liquidity = _to_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidityUSD") or 0)
            volume24 = _to_float(m.get("volume24hr") or m.get("volume_24hr") or m.get("volume") or 0)

            end_date = parse_dt_iso(m.get("endDate") or m.get("end_date") or m.get("closeTime") or "")

            category = m.get("category") or ""
            tags = m.get("tags") or ""
            if isinstance(tags, list):
                tags = ", ".join([str(x) for x in tags[:4]])

            outcomes = []
            if isinstance(m.get("outcomes"), str) and isinstance(m.get("outcomePrices"), str):
                try:
                    names = json.loads(m["outcomes"])
                    prices = json.loads(m["outcomePrices"])
                    for n, p in zip(names, prices):
                        outcomes.append({"name": str(n), "price": _to_float(p)})
                except Exception:
                    pass

            if not question or not outcomes:
                continue

            # choose YES/NO if present; else use leading outcome
            out.append(
                {
                    "id": mid,
                    "question": question,
                    "url": url,
                    "liq": liquidity,
                    "vol24": volume24,
                    "end": end_date.isoformat() if end_date else "",
                    "category": str(category),
                    "tags": str(tags),
                    "outcomes": outcomes,
                }
            )
        except Exception:
            continue

    return out


def pick_binary_yes_price(outcomes: List[Dict[str, Any]]) -> Optional[float]:
    # Return YES price if market has YES/NO; else None
    yes = None
    no = None
    for o in outcomes:
        name = str(o.get("name", "")).strip().upper()
        price = _to_float(o.get("price"))
        if name == "YES":
            yes = price
        elif name == "NO":
            no = price
    if yes is None or no is None:
        return None
    if 0.0001 <= yes <= 0.9999:
        return yes
    return None


# ----------------------------
# CLASSIFICATION
# ----------------------------
def classify_market(m: Dict[str, Any]) -> str:
    q = (m.get("question") or "").lower()
    tags = (m.get("tags") or "").lower()
    cat = (m.get("category") or "").lower()

    # weather/climate
    if any(k in q for k in ["temperature", "rain", "snow", "wind", "hurricane", "storm"]) or "weather" in tags:
        return "weather"

    # crypto
    if any(k in q for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "doge", "price of"]) or "crypto" in tags:
        return "crypto"

    # politics/news
    if any(k in q for k in ["election", "president", "prime minister", "congress", "senate", "bill", "law", "impeach", "resign", "indict", "ceasefire", "war", "nato"]) or "politics" in tags:
        return "politics"

    # default
    return "general_news"


# ----------------------------
# EXTERNAL SIGNALS
# ----------------------------
def fetch_google_news_rss(query: str) -> List[Dict[str, str]]:
    """
    No key. Returns items: title, link, pubDate (raw string)
    """
    url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    r = requests.get(url, timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    root = ET.fromstring(r.text)
    items = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        if title:
            items.append({"title": title, "link": link, "pubDate": pub})
    return items[:12]


def news_probability_heuristic(question: str, state: Dict[str, Any], ts: float) -> Tuple[Optional[float], str]:
    """
    Returns (probability_yes_estimate, explanation)
    Heuristic: evidence score from RSS titles + recency.
    """
    q = clean_title(question, 140)

    # simple query extraction: take key nouns-ish words
    tokens = re.findall(r"[A-Za-z0-9À-ÿ'-]{3,}", question)
    # keep a small set
    key = " ".join(tokens[:7]) if tokens else q

    cache = state.setdefault("cache", {}).setdefault("news", {})
    cached = cache.get(key)
    if cached and (ts - float(cached.get("ts", 0)) < 120):  # 2 min cache
        return cached.get("p"), cached.get("why", "")

    try:
        items = fetch_google_news_rss(key)
    except Exception:
        cache[key] = {"ts": ts, "p": None, "why": "news fetch failed"}
        return None, "news fetch failed"

    # keyword scoring (titles only)
    positive = [
        "confirmed", "passes", "passed", "approved", "signed", "announces", "announced",
        "wins", "win", "agrees", "agreement", "deal reached", "ceasefire agreed",
        "indicted", "resigns", "resigned", "steps down", "appointed"
    ]
    negative = [
        "fails", "blocked", "rejects", "rejected", "denies", "denied", "no evidence",
        "unlikely", "setback", "postponed", "delayed", "falls short"
    ]

    score = 0.0
    hits = 0
    for it in items[:10]:
        t = it["title"].lower()
        s = 0.0
        if any(k in t for k in positive):
            s += 1.0
        if any(k in t for k in negative):
            s -= 1.0
        if s != 0:
            hits += 1
            score += s

    # map score -> probability (conservative!)
    # baseline 0.5; push only if multiple hits
    if hits == 0:
        p = None
        why = "no strong headline evidence"
    else:
        # clamp
        raw = 0.5 + (score / max(3.0, hits * 2.0))  # conservative scaling
        p = float(max(0.1, min(0.9, raw)))
        why = f"news headlines hits={hits}, score={score:.1f}"

    cache[key] = {"ts": ts, "p": p, "why": why}
    return p, why


def open_meteo_max_temp(city: str, ts: float, state: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """
    Uses Open-Meteo geocoding + forecast daily max temp.
    Returns today's/tomorrow's max depending on question not parsed (simple).
    """
    cache = state.setdefault("cache", {}).setdefault("weather", {})
    ck = f"om:{city.lower()}"
    cached = cache.get(ck)
    if cached and (ts - float(cached.get("ts", 0)) < 900):  # 15 min cache
        return cached.get("tmax"), cached.get("why", "")

    try:
        geo = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en", "format": "json"},
            timeout=20,
            headers={"User-Agent": UA},
        ).json()
        res = (geo.get("results") or [])
        if not res:
            cache[ck] = {"ts": ts, "tmax": None, "why": "geocode not found"}
            return None, "geocode not found"
        lat = res[0]["latitude"]
        lon = res[0]["longitude"]

        fc = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "forecast_days": 2,
                "timezone": "UTC",
            },
            timeout=20,
            headers={"User-Agent": UA},
        ).json()

        temps = (((fc.get("daily") or {}).get("temperature_2m_max")) or [])
        if not temps:
            cache[ck] = {"ts": ts, "tmax": None, "why": "forecast missing"}
            return None, "forecast missing"

        # pick next-day max (more useful for "tomorrow" markets)
        tmax = float(temps[1]) if len(temps) > 1 else float(temps[0])
        why = f"Open-Meteo max≈{tmax:.1f}C (next day)"
        cache[ck] = {"ts": ts, "tmax": tmax, "why": why}
        return tmax, why
    except Exception:
        cache[ck] = {"ts": ts, "tmax": None, "why": "weather fetch failed"}
        return None, "weather fetch failed"


def weather_probability_heuristic(question: str, outcomes: List[Dict[str, Any]], state: Dict[str, Any], ts: float) -> Tuple[Optional[float], str]:
    """
    For weather markets, we often don't have buckets reliably in Gamma,
    so we approximate only for binary style like:
      "Will the highest temperature in London be above 10C?"
    If question doesn't match, returns None.
    """
    q = question.lower()

    m = re.search(r"above\s+(-?\d+)\s*°?\s*c", q)
    if not m:
        m = re.search(r"over\s+(-?\d+)\s*°?\s*c", q)
    if not m:
        return None, "weather: no 'above X C' pattern"

    threshold = float(m.group(1))

    # city: very rough extraction
    city = None
    for c in ["london", "paris", "berlin", "vienna", "amsterdam", "madrid", "rome", "lisbon"]:
        if c in q:
            city = c.title()
            break
    if not city:
        return None, "weather: city not detected"

    tmax, why_t = open_meteo_max_temp(city, ts, state)
    if tmax is None:
        return None, "weather: forecast unavailable"

    # simple prob: if forecast above threshold by margin, increase probability
    margin = tmax - threshold
    # conservative mapping
    if margin >= 2.0:
        p = 0.75
    elif margin >= 1.0:
        p = 0.62
    elif margin >= 0.0:
        p = 0.52
    elif margin >= -1.0:
        p = 0.40
    else:
        p = 0.28

    return float(p), f"weather: {why_t}, threshold={threshold}, margin={margin:+.1f}C"


def coingecko_price_history(coin_id: str, days: int = 30) -> List[float]:
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    r = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=25, headers={"User-Agent": UA})
    r.raise_for_status()
    data = r.json()
    prices = [float(p[1]) for p in (data.get("prices") or []) if isinstance(p, list) and len(p) >= 2]
    return prices


def crypto_probability_heuristic(question: str, state: Dict[str, Any], ts: float) -> Tuple[Optional[float], str]:
    """
    Handles basic binary like:
    - "Will Bitcoin reach $90,000 in February?"
    - "Will BTC be above $80,000 on Feb 3?"
    Returns probability estimate using spot + realized vol approximation (very rough).
    """
    q = question.lower()
    if "bitcoin" in q or re.search(r"\bbtc\b", q):
        coin_id = "bitcoin"
        sym = "BTC"
    elif "ethereum" in q or re.search(r"\beth\b", q):
        coin_id = "ethereum"
        sym = "ETH"
    else:
        return None, "crypto: coin not detected"

    # target price
    m = re.search(r"\$?\s*([0-9]{2,3}(?:[,][0-9]{3})+|[0-9]{4,6})", question.replace("USD", "$"))
    if not m:
        return None, "crypto: target not detected"
    target = float(m.group(1).replace(",", ""))

    # cache history briefly
    cache = state.setdefault("cache", {}).setdefault("crypto", {})
    ck = f"cg:{coin_id}"
    cached = cache.get(ck)
    if cached and (ts - float(cached.get("ts", 0)) < 600):  # 10 min
        spot = float(cached["spot"])
        vol = float(cached["vol"])
    else:
        prices = coingecko_price_history(coin_id, 30)
        if len(prices) < 10:
            return None, "crypto: not enough history"
        spot = float(prices[-1])
        # realized daily log-return std
        rets = []
        # sample every ~24h: use rough downsample
        step = max(1, len(prices) // 30)
        sampled = prices[::step]
        for i in range(1, len(sampled)):
            r = math.log(sampled[i] / sampled[i - 1])
            rets.append(r)
        if len(rets) < 5:
            return None, "crypto: vol calc failed"
        vol = float(_std(rets))  # daily vol in log terms
        cache[ck] = {"ts": ts, "spot": spot, "vol": vol}

    # time horizon rough: if question contains a date, assume 7 days else 21 days
    horizon_days = 21
    if re.search(r"\bfeb\b|\bmar\b|\bapr\b|\bjan\b|\b2026\b", q):
        horizon_days = 10  # keep conservative short horizon by default

    # probability BTC >= target at horizon using lognormal approx:
    # P(S_T >= K) where ln(S_T) ~ N(ln(S0), vol*sqrt(T))
    T = max(1.0, float(horizon_days))
    sigma = max(0.0001, vol * math.sqrt(T))
    mu = math.log(spot)

    z = (math.log(target) - mu) / sigma
    p = 1.0 - _norm_cdf(z)

    # clamp
    p = float(max(0.05, min(0.95, p)))
    return p, f"crypto: {sym} spot≈{spot:.0f}, target={target:.0f}, vol(daily)≈{vol:.3f}, horizon≈{horizon_days}d"


def _std(xs: List[float]) -> float:
    m = sum(xs) / len(xs)
    v = sum((x - m) ** 2 for x in xs) / max(1, (len(xs) - 1))
    return math.sqrt(v)


def _norm_cdf(x: float) -> float:
    # Abramowitz/Stegun approx using erf
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ----------------------------
# EDGE + RECOMMENDATION
# ----------------------------
def compute_edge(market_p_yes: float, ext_p_yes: float) -> float:
    # in probability points (0-100)
    return (ext_p_yes - market_p_yes) * 100.0


def action_text(edge_pp: float) -> str:
    # edge_pp positive -> YES undervalued (buy YES)
    # edge_pp negative -> NO undervalued (buy NO)
    if edge_pp >= 0:
        return "✅ ACTION: consider entry now → leaning YES"
    return "✅ ACTION: consider entry now → leaning NO"


def build_reason(cat: str, market_p: float, ext_p: float, edge_pp: float, why: str, liq: float) -> str:
    return (
        f"🧠 Reason: Cat={cat} | MarketP(YES)={market_p:.3f} | ExtP(YES)={ext_p:.3f} | "
        f"Edge={edge_pp:+.1f}pp | Liq={int(liq)} | {why}"
    )


# ----------------------------
# MAIN LOOP
# ----------------------------
def main() -> None:
    state = load_state()
    tg_send("🤖 Bot ligado: ACTION-only + Edge (news/weather/crypto).")

    while True:
        ts = time.time()
        try:
            markets = fetch_markets(limit=220)
        except Exception as e:
            print("fetch error:", e)
            time.sleep(SCAN_SECONDS)
            continue

        candidates: List[Dict[str, Any]] = []

        for m in markets:
            try:
                if float(m.get("liq", 0)) < MIN_LIQ:
                    continue

                p_yes = pick_binary_yes_price(m.get("outcomes", []))
                if p_yes is None:
                    # skip non-binary for now (you can extend later)
                    continue

                cat = classify_market(m)
                q = m["question"]

                ext_p = None
                why = ""

                if cat == "weather":
                    ext_p, why = weather_probability_heuristic(q, m.get("outcomes", []), state, ts)
                elif cat == "crypto":
                    ext_p, why = crypto_probability_heuristic(q, state, ts)
                elif cat in ("politics", "general_news"):
                    ext_p, why = news_probability_heuristic(q, state, ts)

                if ext_p is None:
                    continue

                edge_pp = compute_edge(p_yes, ext_p)

                # Only act on meaningful divergence
                if abs(edge_pp) < EDGE_MIN_PCT:
                    # decay confirm counter if any
                    state.setdefault("confirm", {})
                    if m["id"] in state["confirm"]:
                        state["confirm"][m["id"]] = max(0, int(state["confirm"][m["id"]]) - 1)
                    continue

                # confirmation: require consecutive hits
                state.setdefault("confirm", {})
                state["confirm"][m["id"]] = int(state["confirm"].get(m["id"], 0)) + 1
                if int(state["confirm"][m["id"]]) < CONFIRM_SCANS:
                    continue

                candidates.append(
                    {
                        "id": m["id"],
                        "url": m.get("url", ""),
                        "question": m["question"],
                        "cat": cat,
                        "liq": float(m.get("liq", 0)),
                        "p_yes": float(p_yes),
                        "ext_p": float(ext_p),
                        "edge_pp": float(edge_pp),
                        "why": why,
                    }
                )
            except Exception:
                continue

        # prioritize biggest absolute edge, then liquidity
        candidates.sort(key=lambda x: (abs(x["edge_pp"]), x["liq"]), reverse=True)

        sent = 0
        for c in candidates:
            if sent >= MAX_ALERTS_PER_CYCLE:
                break
            if not global_hourly_cap_ok(state, ts):
                break
            if not cooldown_ok(state, c["id"], ts):
                continue

            msg = (
                "🚨 ACTION ⬇️\n"
                f"🎯 {action_text(c['edge_pp'])}\n"
                f"{build_reason(c['cat'], c['p_yes'], c['ext_p'], c['edge_pp'], c['why'], c['liq'])}\n"
                f"📌 {clean_title(c['question'])}\n"
                f"{c['url']}"
            )
            tg_send(msg)
            mark_alert(state, c["id"], ts)
            sent += 1

        save_state(state)
        time.sleep(SCAN_SECONDS)


if __name__ == "__main__":
    main()
