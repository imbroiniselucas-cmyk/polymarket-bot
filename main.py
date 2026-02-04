#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import requests
import re

# ======================================================
# POLYMARKET INFORMATION-GAP BOT (LEVEL 1)
# - Detects info gaps vs external sources
# - Weather (forecast vs odds)
# - News-driven (crypto / politics / macro)
# - Telegram alerts ONLY
# ======================================================

GAMMA_API = "https://gamma-api.polymarket.com"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "180"))  # 3 min
STATE_FILE = "state.json"

session = requests.Session()
session.headers.update({"User-Agent": "pm-info-gap-bot/1.0"})


# ------------------------------------------------------
# Telegram
# ------------------------------------------------------
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    session.post(url, json={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    })


# ------------------------------------------------------
# State
# ------------------------------------------------------
def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ------------------------------------------------------
# Polymarket helpers
# ------------------------------------------------------
def fetch_markets():
    r = session.get(GAMMA_API + "/markets", params={"limit": 200}, timeout=20)
    r.raise_for_status()
    return r.json()


def extract_yes_price(m):
    for key in ("outcomes", "tokens"):
        arr = m.get(key)
        if isinstance(arr, list):
            for o in arr:
                if (o.get("name") or "").lower() == "yes":
                    try:
                        return float(o.get("price"))
                    except:
                        return None
    return None


# ------------------------------------------------------
# WEATHER INTELLIGENCE
# ------------------------------------------------------
def detect_weather_market(question: str):
    """
    Very simple NLP:
    Looks for temperature + city + date
    """
    q = question.lower()

    if "temperature" not in q and "temp" not in q:
        return None

    city_match = re.search(r"in ([a-zA-Z ]+)", q)
    temp_match = re.search(r"(\d+)\s?°?\s?c", q)

    if not city_match or not temp_match:
        return None

    city = city_match.group(1).strip()
    threshold = int(temp_match.group(1))

    return {
        "city": city,
        "threshold": threshold
    }


def fetch_weather_forecast(city: str):
    """
    Uses Open-Meteo (no API key)
    """
    try:
        geo = session.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1},
            timeout=10
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
                "timezone": "UTC"
            },
            timeout=10
        ).json()

        temps = weather.get("daily", {}).get("temperature_2m_max", [])
        if not temps:
            return None

        return max(temps[:1])  # next day max
    except:
        return None


# ------------------------------------------------------
# NEWS INTELLIGENCE (crypto / politics / macro)
# ------------------------------------------------------
def fetch_news_headlines(query: str):
    """
    Google News RSS (no API key)
    """
    try:
        r = session.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": "en", "gl": "US"},
            timeout=10
        )
        headlines = []
        for part in r.text.split("<title>")[2:5]:
            headlines.append(part.split("</title>")[0])
        return headlines
    except:
        return []


# ------------------------------------------------------
# MAIN LOOP
# ------------------------------------------------------
def main():
    state = load_state()
    tg("🧠 Polymarket INFO-GAP bot online.\nScanning markets vs real-world information.")

    while True:
        try:
            markets = fetch_markets()

            for m in markets:
                market_id = str(m.get("id"))
                if not market_id or market_id in state:
                    continue

                question = m.get("question", "")
                yes_price = extract_yes_price(m)
                if yes_price is None:
                    continue

                implied_prob = yes_price * 100
                slug = m.get("slug", "")
                url = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"

                # ---------------- WEATHER GAP ----------------
                weather = detect_weather_market(question)
                if weather:
                    forecast = fetch_weather_forecast(weather["city"])
                    if forecast and forecast > weather["threshold"] + 0.5:
                        tg(
                            f"🌦️ <b>INFORMATION GAP (WEATHER)</b>\n\n"
                            f"<b>{question}</b>\n\n"
                            f"Market YES: <b>{implied_prob:.1f}%</b>\n"
                            f"Forecast max temp: <b>{forecast:.1f}°C</b>\n"
                            f"Threshold: <b>{weather['threshold']}°C</b>\n\n"
                            f"Interpretation:\n"
                            f"• Forecast ABOVE market threshold\n"
                            f"• Market may be underpricing YES\n\n"
                            f"Action:\n"
                            f"• Check resolution rules\n"
                            f"• Compare with 1–2 other forecasts\n\n"
                            f"{url}"
                        )
                        state[market_id] = True
                        continue

                # ---------------- NEWS GAP ----------------
                headlines = fetch_news_headlines(question[:80])
                if headlines and implied_prob < 50:
                    tg(
                        f"📰 <b>POSSIBLE INFO GAP (NEWS)</b>\n\n"
                        f"<b>{question}</b>\n\n"
                        f"Market YES: <b>{implied_prob:.1f}%</b>\n\n"
                        f"Recent headlines:\n"
                        + "\n".join([f"• {h}" for h in headlines]) +
                        f"\n\nInterpretation:\n"
                        f"• News exists but market may not have repriced yet\n"
                        f"• Requires human judgment\n\n"
                        f"{url}"
                    )
                    state[market_id] = True

            save_state(state)
            time.sleep(POLL_SECONDS)

        except Exception as e:
            tg(f"❌ Bot error:\n{str(e)[:200]}")
            time.sleep(60)


if __name__ == "__main__":
    main()
