#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import requests
import re
from html import escape

# ======================================================
# POLYMARKET INFORMATION-GAP BOT (LEVEL 1.5)
# - Info gaps (weather/news) + simple single-market arb
# - Two alert tiers:
#     🟡 WATCH  (small edge, more alerts)
#     🟢 ACTION (bigger edge)
# - Cooldown + re-alert only if edge improves
# - Clear ACTION + WHY messages
# ======================================================

GAMMA_API = "https://gamma-api.polymarket.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))  # 3 min
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Thresholds (tune here or via env vars)
EDGE_SIGNAL = float(os.getenv("EDGE_SIGNAL", "0.01"))  # 1% (watch)
EDGE_ACTION = float(os.getenv("EDGE_ACTION", "0.03"))  # 3% (action)

SIGNAL_COOLDOWN_SECONDS = int(os.getenv("SIGNAL_COOLDOWN_SECONDS", "2700"))  # 45 min
SIGNAL_RE_ALERT_IMPROVE = float(os.getenv("SIGNAL_RE_ALERT_IMPROVE", "0.005"))  # +0.5%

ACTION_COOLDOWN_SECONDS = int(os.getenv("ACTION_COOLDOWN_SECONDS", "7200"))  # 2h
ACTION_RE_ALERT_IMPROVE = float(os.getenv("ACTION_RE_ALERT_IMPROVE", "0.02"))  # +2%

# For "sum of outcomes" sanity checks in multi-outcome markets
# If sum deviates from 1.0 by >= threshold, consider it an edge.
ARB_SUM_EDGE_SIGNAL = float(os.getenv("ARB_SUM_EDGE_SIGNAL", "0.01"))
ARB_SUM_EDGE_ACTION = float(os.getenv("ARB_SUM_EDGE_ACTION", "0.03"))

session = requests.Session()
session.headers.update({"User-Agent": "pm-info-gap-bot/1.5"})


# ------------------------------------------------------
# Telegram
# ------------------------------------------------------
def tg(msg: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        session.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception:
        # Avoid crash loops if Telegram is down
        print("Telegram send failed.")


# ------------------------------------------------------
# State
# ------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def should_alert(mstate, level: str, edge: float, now_ts: int) -> bool:
    if mstate is None:
        mstate = {}

    if level == "signal":
        last_ts = int(mstate.get("signal_last_ts", 0) or 0)
        best = float(mstate.get("best_signal_edge", 0.0) or 0.0)
        if (now_ts - last_ts) >= SIGNAL_COOLDOWN_SECONDS:
            return True
        if edge >= best + SIGNAL_RE_ALERT_IMPROVE:
            return True
        return False

    if level == "action":
        last_ts = int(mstate.get("action_last_ts", 0) or 0)
        best = float(mstate.get("best_action_edge", 0.0) or 0.0)
        if (now_ts - last_ts) >= ACTION_COOLDOWN_SECONDS:
            return True
        if edge >= best + ACTION_RE_ALERT_IMPROVE:
            return True
        return False

    return False


def record_alert(state: dict, market_id: str, level: str, edge: float, now_ts: int):
    mstate = state.get(market_id, {}) or {}
    if level == "signal":
        mstate["signal_last_ts"] = now_ts
        mstate["best_signal_edge"] = max(float(mstate.get("best_signal_edge", 0.0) or 0.0), edge)
    elif level == "action":
        mstate["action_last_ts"] = now_ts
        mstate["best_action_edge"] = max(float(mstate.get("best_action_edge", 0.0) or 0.0), edge)
    state[market_id] = mstate


# ------------------------------------------------------
# Polymarket helpers (Gamma API)
# ------------------------------------------------------
def fetch_markets(limit=200):
    r = session.get(GAMMA_API + "/markets", params={"limit": limit}, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_outcome_prices(m):
    """
    Returns list of (name, price_float) for all outcomes that have a price.
    Tries "outcomes" then "tokens".
    """
    prices = []
    for key in ("outcomes", "tokens"):
        arr = m.get(key)
        if isinstance(arr, list) and arr:
            for o in arr:
                name = (o.get("name") or o.get("title") or "").strip()
                p = o.get("price")
                try:
                    pf = float(p)
                except Exception:
                    continue
                if name and 0.0 <= pf <= 1.0:
                    prices.append((name, pf))
            if prices:
                break
    return prices


def market_url(m):
    slug = m.get("slug", "")
    return f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"


# ------------------------------------------------------
# WEATHER INTELLIGENCE
# ------------------------------------------------------
def detect_weather_market(question: str):
    """
    Basic parsing for temperature threshold questions.
    Tries to infer:
      - city
      - threshold C
      - direction: "above" or "below" (default: above)
    """
    q = question.lower()

    if not any(k in q for k in ("temperature", "temp", "°c", "celsius")):
        return None

    # Direction
    direction = "above"
    if any(k in q for k in ("below", "under", "at most", "no more than", "less than")):
        direction = "below"
    if any(k in q for k in ("above", "over", "at least", "more than", "greater than")):
        direction = "above"

    # City: naive "in CITY"
    city_match = re.search(r"\bin ([a-zA-ZÀ-ÿ \-']+)", q)
    # Threshold: "10 C" or "10°C"
    temp_match = re.search(r"(\d{1,2})\s?°?\s?c", q)

    if not city_match or not temp_match:
        return None

    city = city_match.group(1).strip()
    threshold = int(temp_match.group(1))

    return {"city": city, "threshold": threshold, "direction": direction}


def fetch_weather_forecast_next_day_max(city: str):
    """
    Open-Meteo (no API key)
    Returns next day's max temp (°C) in UTC timezone daily forecast.
    """
    try:
        geo = session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=10,
        ).json()

        if not geo.get("results"):
            return None

        lat = geo["results"][0]["latitude"]
        lon = geo["results"][0]["longitude"]

        weather = session.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "timezone": "UTC",
            },
            timeout=10,
        ).json()

        temps = weather.get("daily", {}).get("temperature_2m_max", [])
        if not temps:
            return None

        # next day
        return float(temps[0])
    except Exception:
        return None


# ------------------------------------------------------
# NEWS INTELLIGENCE (RSS, no API key)
# ------------------------------------------------------
_title_re = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_cdata_re = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _clean_rss_title(t: str) -> str:
    t = t.strip()
    m = _cdata_re.search(t)
    if m:
        t = m.group(1).strip()
    # Google News titles often include " - Source"
    t = re.sub(r"\s+-\s+.*$", "", t).strip()
    return t


def fetch_news_headlines(query: str, limit=3):
    """
    Google News RSS. Returns up to `limit` titles.
    Lightweight parsing to avoid extra dependencies.
    """
    try:
        r = session.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en", "gl": "US", "ceid": "US:en"},
            timeout=10,
        )
        r.raise_for_status()
        titles = _title_re.findall(r.text)
        # First title is channel title; skip it
        cleaned = []
        for t in titles[1:]:
            ct = _clean_rss_title(t)
            if ct and ct not in cleaned:
                cleaned.append(ct)
            if len(cleaned) >= limit:
                break
        return cleaned
    except Exception:
        return []


# ------------------------------------------------------
# SIMPLE SINGLE-MARKET ARB CHECK
# ------------------------------------------------------
def compute_sum_edge(prices):
    """
    For multi-outcome markets, sum of all outcomes should be ~ 1.0.
    Edge = abs(1 - sum_prices)
    """
    s = sum(p for _, p in prices)
    edge = abs(1.0 - s)
    return s, edge


# ------------------------------------------------------
# Alert builders
# ------------------------------------------------------
def fmt_level(edge: float):
    if edge >= EDGE_ACTION:
        return "action"
    if edge >= EDGE_SIGNAL:
        return "signal"
    return None


def level_badge(level: str):
    return "🟢 <b>ACTION</b>" if level == "action" else "🟡 <b>WATCH</b>"


def safe(s: str) -> str:
    return escape(s or "")


# ------------------------------------------------------
# MAIN LOOP
# ------------------------------------------------------
def main():
    state = load_state()
    tg("🧠 Polymarket INFO-GAP bot online.\n🟡 Watch (small edges) + 🟢 Action (bigger edges).")

    while True:
        now = int(time.time())
        try:
            markets = fetch_markets(limit=200)

            for m in markets:
                market_id = str(m.get("id") or "")
                if not market_id:
                    continue

                question = (m.get("question") or "").strip()
                if not question:
                    continue

                url = market_url(m)

                # Ensure per-market state is a dict
                mstate = state.get(market_id, {}) or {}

                # ------------- 1) SINGLE-MARKET SUM CHECK -------------
                prices = extract_outcome_prices(m)
                if len(prices) >= 2:
                    s, edge = compute_sum_edge(prices)

                    # separate thresholds for sum-edge
                    sum_level = None
                    if edge >= ARB_SUM_EDGE_ACTION:
                        sum_level = "action"
                    elif edge >= ARB_SUM_EDGE_SIGNAL:
                        sum_level = "signal"

                    if sum_level:
                        if should_alert(mstate, sum_level, edge, now):
                            top = sorted(prices, key=lambda x: x[1], reverse=True)[:4]
                            lines = "\n".join([f"• {safe(n)}: <b>{p*100:.1f}%</b>" for n, p in top])

                            tg(
                                f"⚖️ {level_badge(sum_level)} <b>SUM DEVIATION</b>\n\n"
                                f"<b>{safe(question)}</b>\n\n"
                                f"Sum(outcomes): <b>{s*100:.1f}%</b>\n"
                                f"Deviation edge: <b>{edge*100:.2f}%</b>\n\n"
                                f"Top outcomes:\n{lines}\n\n"
                                f"Why it matters:\n"
                                f"• Multi-outcome markets often imply probabilities should sum ~ 100%\n"
                                f"• Deviations can indicate mispricing or stale quotes\n\n"
                                f"Action:\n"
                                f"• Open market + check liquidity/spread\n"
                                f"• Verify resolution rules\n\n"
                                f"{safe(url)}"
                            )
                            record_alert(state, market_id, sum_level, edge, now)
                            mstate = state.get(market_id, {}) or {}

                # ------------- 2) WEATHER GAP -------------
                weather = detect_weather_market(question)
                if weather:
                    forecast = fetch_weather_forecast_next_day_max(weather["city"])
                    if forecast is not None:
                        # Market's "YES" price (if exists)
                        yes_price = None
                        for name, p in prices:
                            if name.lower() == "yes":
                                yes_price = p
                                break

                        # If no YES price, skip weather (can't compare)
                        if yes_price is not None:
                            implied = yes_price
                            thr = weather["threshold"]
                            direction = weather["direction"]

                            # Rough probability heuristic:
                            # if direction is "above" and forecast > thr + 0.5 => gap
                            # if direction is "below" and forecast < thr - 0.5 => gap
                            gap_ok = False
                            if direction == "above" and forecast > thr + 0.5:
                                gap_ok = True
                            if direction == "below" and forecast < thr - 0.5:
                                gap_ok = True

                            # Edge estimate: how far the market is from a naive "forecast-leaning" belief.
                            # This is NOT true probability; it’s just for tiering alerts.
                            # If forecast supports YES strongly, assume "fair" ~ 0.65 else ~0.35 (simple).
                            fair = 0.65 if gap_ok else 0.50
                            edge_est = abs(fair - implied)

                            lvl = fmt_level(edge_est)
                            if gap_ok and lvl:
                                if should_alert(mstate, lvl, edge_est, now):
                                    arrow = "ABOVE" if direction == "above" else "BELOW"
                                    tg(
                                        f"🌦️ {level_badge(lvl)} <b>WEATHER GAP</b>\n\n"
                                        f"<b>{safe(question)}</b>\n\n"
                                        f"Market YES: <b>{implied*100:.1f}%</b>\n"
                                        f"Forecast next-day max: <b>{forecast:.1f}°C</b>\n"
                                        f"Threshold: <b>{thr}°C</b> ({arrow})\n\n"
                                        f"Why:\n"
                                        f"• Forecast suggests the YES condition is more likely than price implies\n\n"
                                        f"Action:\n"
                                        f"• Check market resolution (station, timezone, max vs avg)\n"
                                        f"• Compare with 1–2 other forecasts\n\n"
                                        f"{safe(url)}"
                                    )
                                    record_alert(state, market_id, lvl, edge_est, now)
                                    mstate = state.get(market_id, {}) or {}

                # ------------- 3) NEWS GAP (lightweight) -------------
                # Only do this for a subset (avoid spamming/requests):
                # - If the question looks like news-sensitive
                ql = question.lower()
                if any(k in ql for k in ("trump", "election", "fed", "rate", "bitcoin", "ethereum", "sec", "cpi", "inflation")):
                    # If we have a YES price, use it to tier;
                    yes_price = None
                    for name, p in prices:
                        if name.lower() == "yes":
                            yes_price = p
                            break

                    # Pull a few headlines based on first part of question
                    headlines = fetch_news_headlines(question[:90], limit=3)

                    if headlines:
                        # If market is < 50%, treat it as "maybe underpriced" signal.
                        # Edge estimate: distance from 0.5 (simple heuristic).
                        implied = yes_price if yes_price is not None else 0.5
                        edge_est = abs(0.5 - implied)

                        # Use lower bar for WATCH on news
                        lvl = "action" if edge_est >= EDGE_ACTION else ("signal" if edge_est >= EDGE_SIGNAL else None)
                        if lvl and should_alert(mstate, lvl, edge_est, now):
                            tg(
                                f"📰 {level_badge(lvl)} <b>NEWS CONTEXT</b>\n\n"
                                f"<b>{safe(question)}</b>\n\n"
                                f"Market YES: <b>{implied*100:.1f}%</b>\n\n"
                                f"Recent headlines:\n"
                                + "\n".join([f"• {safe(h)}" for h in headlines])
                                + "\n\n"
                                f"How to use this:\n"
                                f"• Headlines can move probability fast; watch liquidity/spread\n"
                                f"• Don’t trade without checking resolution rules\n\n"
                                f"{safe(url)}"
                            )
                            record_alert(state, market_id, lvl, edge_est, now)

            save_state(state)
            time.sleep(POLL_SECONDS)

        except Exception as e:
            tg(f"❌ Bot error:\n{safe(str(e)[:300])}")
            time.sleep(60)


if __name__ == "__main__":
    main()
