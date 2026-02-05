#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Alert Bot (AGGRESSIVE_EDGE v3)
- MORE alerts (lower thresholds)
- Recommendation ALWAYS explicit:
    ✅ ENTER YES (A FAVOR)  or  ✅ ENTER NO (CONTRA)  or  WAIT
- Weather/Climate edge engine for: London, Buenos Aires, Ankara
- Shows MONEY FLOW MOVE% (volume delta % since last scan)
- Shows PRICE MOVE% (YES price move % since last scan)
- No health/status spam

REQUIRED ENV:
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID

OPTIONAL ENV:
  POLY_ENDPOINT
  SCAN_EVERY_SEC=180
  MAX_MARKETS=350
  COOLDOWN_SEC=600
  REARM_PRICE_MOVE_PCT=1.0
  REARM_VOL_MOVE_PCT=8.0

  # General thresholds
  GAP_MIN=0.006
  SCORE_MIN=6.0
  VOL_MIN=8000
  LIQ_MIN=4000

  # Weather edge settings
  WEATHER_EDGE_MIN=0.08          # minimum edge to enter (8pp)
  WEATHER_SIGMA_C=1.8            # uncertainty used to turn point forecast into probability (temp)
"""

import os
import time
import math
import re
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

# ----------------------------
# Telegram
# ----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

try:
    import telebot  # type: ignore
    _HAS_TELEBOT = True
except Exception:
    _HAS_TELEBOT = False


def send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return

    msg = msg.strip()
    if not msg:
        return

    if _HAS_TELEBOT:
        bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)
        bot.send_message(TELEGRAM_CHAT_ID, msg, disable_web_page_preview=True)
    else:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "disable_web_page_preview": True}
        requests.post(url, json=payload, timeout=15).raise_for_status()


# ----------------------------
# Config
# ----------------------------
POLY_ENDPOINT = os.getenv("POLY_ENDPOINT", "").strip()

SCAN_EVERY_SEC = int(os.getenv("SCAN_EVERY_SEC", "180"))
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "350"))
COOLDOWN_SEC = int(os.getenv("COOLDOWN_SEC", "600"))  # 10 min

REARM_PRICE_MOVE_PCT = float(os.getenv("REARM_PRICE_MOVE_PCT", "1.0"))  # bypass cooldown if price moved > 1%
REARM_VOL_MOVE_PCT = float(os.getenv("REARM_VOL_MOVE_PCT", "8.0"))      # bypass cooldown if volume moved > 8%

GAP_MIN = float(os.getenv("GAP_MIN", "0.006"))       # 0.6%
SCORE_MIN = float(os.getenv("SCORE_MIN", "6.0"))
VOL_MIN = float(os.getenv("VOL_MIN", "8000"))
LIQ_MIN = float(os.getenv("LIQ_MIN", "4000"))

WEATHER_EDGE_MIN = float(os.getenv("WEATHER_EDGE_MIN", "0.08"))
WEATHER_SIGMA_C = float(os.getenv("WEATHER_SIGMA_C", "1.8"))

HTTP_TIMEOUT = 20
UA = {"User-Agent": "AGGRESSIVE_EDGE_BOT/3.0"}

# Cities (fixed list you asked for)
CITY_DB = {
    "london": {"name": "London", "lat": 51.5072, "lon": -0.1276, "tz": "Europe/London"},
    "buenos aires": {"name": "Buenos Aires", "lat": -34.6037, "lon": -58.3816, "tz": "America/Argentina/Buenos_Aires"},
    "ankara": {"name": "Ankara", "lat": 39.9334, "lon": 32.8597, "tz": "Europe/Istanbul"},
}


# ----------------------------
# Helpers
# ----------------------------
def _to_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip()
        if s == "":
            return default
        return float(s)
    except Exception:
        return default


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _title(m: Dict[str, Any]) -> str:
    for k in ["title", "question", "name", "marketTitle"]:
        if k in m and m.get(k):
            return _clean(str(m.get(k)))
    ev = m.get("event") or {}
    if isinstance(ev, dict) and ev.get("title"):
        return _clean(str(ev.get("title")))
    return "Untitled market"


def _url(m: Dict[str, Any]) -> str:
    for k in ["url", "marketUrl", "market_url", "link"]:
        if k in m and m.get(k):
            return str(m.get(k)).strip()
    slug = m.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = m.get("id") or m.get("marketId") or m.get("market_id")
    if mid:
        return f"https://polymarket.com/event/{mid}"
    return "https://polymarket.com/markets"


def _market_key(m: Dict[str, Any]) -> str:
    for k in ["id", "marketId", "market_id", "conditionId", "condition_id", "slug"]:
        if k in m and m.get(k):
            return str(m.get(k))
    return _title(m)[:80]


def _best_yes_price(m: Dict[str, Any]) -> Optional[float]:
    outcomes = m.get("outcomes")
    outcome_prices = m.get("outcomePrices") or m.get("outcome_prices")

    if outcomes and outcome_prices and isinstance(outcomes, list) and isinstance(outcome_prices, list):
        idx = None
        for i, o in enumerate(outcomes):
            if str(o).strip().lower() == "yes":
                idx = i
                break
        if idx is not None and idx < len(outcome_prices):
            p = _to_float(outcome_prices[idx], default=-1.0)
            if 0.0 <= p <= 1.0:
                return p

    for key in ["yesPrice", "yes_price", "p_yes", "probYes", "lastTradePrice", "last_trade_price", "last", "lastPrice"]:
        if key in m:
            p = _to_float(m.get(key), default=-1.0)
            if 0.0 <= p <= 1.0:
                return p

    return None


def _best_bid_ask(m: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    bid_keys = ["bestBid", "best_bid", "bid", "yesBid", "yes_bid"]
    ask_keys = ["bestAsk", "best_ask", "ask", "yesAsk", "yes_ask"]
    bid = None
    ask = None
    for k in bid_keys:
        if k in m:
            v = _to_float(m.get(k), default=-1.0)
            if 0.0 <= v <= 1.0:
                bid = v
                break
    for k in ask_keys:
        if k in m:
            v = _to_float(m.get(k), default=-1.0)
            if 0.0 <= v <= 1.0:
                ask = v
                break
    return bid, ask


def _spread(m: Dict[str, Any]) -> float:
    bid, ask = _best_bid_ask(m)
    if bid is None or ask is None:
        return 0.0
    return max(0.0, ask - bid)


def _volume(m: Dict[str, Any]) -> float:
    for k in ["volume", "volume24hr", "volume24h", "volume_24h", "volumeUsd", "volumeUSD", "volume_usd"]:
        if k in m:
            v = _to_float(m.get(k), default=0.0)
            if v > 0:
                return v
    v = m.get("volume")
    if isinstance(v, dict):
        for kk in ["usd", "USD", "24h", "24hr"]:
            if kk in v:
                vv = _to_float(v.get(kk), default=0.0)
                if vv > 0:
                    return vv
    return 0.0


def _liquidity(m: Dict[str, Any]) -> float:
    for k in ["liquidity", "liquidityUSD", "liquidityUsd", "liquidity_usd", "openInterest", "open_interest"]:
        if k in m:
            v = _to_float(m.get(k), default=0.0)
            if v > 0:
                return v
    return 0.0


def _score(vol: float, liq: float, spread: float, move_abs: float) -> float:
    vol_term = math.log10(max(vol, 1.0))
    liq_term = math.log10(max(liq, 1.0))
    spread_term = (spread * 100.0) * 0.9
    move_term = (move_abs * 100.0) * 0.7
    base = 1.1
    s = base + vol_term + liq_term + spread_term + move_term
    return float(_clamp(s, 0.0, 20.0))


def _tier(score: float, spread: float, move_abs: float) -> str:
    if score >= 10.0 or spread >= 0.02 or move_abs >= 0.03:
        return "STRONG"
    if score >= 7.2:
        return "TACTICAL"
    return "WEAK"


# ----------------------------
# Weather/Climate engine
# ----------------------------
def is_weather_market(title: str) -> bool:
    t = title.lower()
    city_hit = any(c in t for c in CITY_DB.keys())
    if not city_hit:
        return False
    keys = [
        "temperature", "temp", "high", "low", "max", "minimum", "maximum",
        "rain", "precip", "precipitation", "snow", "wind", "°c", "°f",
        "mm", "cm", "inches", "mph", "km/h"
    ]
    return any(k in t for k in keys)


def _parse_city(title: str) -> Optional[dict]:
    t = title.lower()
    for k, v in CITY_DB.items():
        if k in t:
            return v
    return None


def _parse_date(title: str) -> str:
    """
    Best-effort:
      - "today" => today
      - "tomorrow" => today+1
      - otherwise default to tomorrow (common in weather markets)
    """
    t = title.lower()
    today = dt.date.today()
    if "today" in t:
        return today.isoformat()
    if "tomorrow" in t:
        return (today + dt.timedelta(days=1)).isoformat()
    # try parse month/day like "Feb 6" / "February 6"
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\s+(\d{1,2})\b", t)
    if m:
        mon = m.group(1)[:3]
        day = int(m.group(2))
        month_map = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                     "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        year = today.year
        month = month_map.get(mon, today.month)
        try:
            d = dt.date(year, month, day)
            # if already passed, assume next year
            if d < today:
                d = dt.date(year + 1, month, day)
            return d.isoformat()
        except Exception:
            pass
    return (today + dt.timedelta(days=1)).isoformat()


def _parse_metric_threshold(title: str) -> Tuple[str, float, str]:
    """
    metric: tmax/tmin/precip/windmax
    units: C/F/mm
    op: >= or <=
    """
    t = title.lower()

    # op
    if any(w in t for w in ["below", "under", "less than", "at most", "no more than"]):
        op = "<="
    else:
        op = ">="

    # metric
    if any(w in t for w in ["low", "min", "minimum"]):
        metric = "tmin"
    elif any(w in t for w in ["high", "max", "maximum"]):
        metric = "tmax"
    elif any(w in t for w in ["rain", "precip", "precipitation", "snow"]):
        metric = "precip"
    elif "wind" in t:
        metric = "windmax"
    else:
        metric = "tmax"

    # units
    units = "C"
    if "°f" in t or "fahrenheit" in t:
        units = "F"
    if metric == "precip":
        # keep mm-ish. If inches appears, we still handle it.
        if "inch" in t or "inches" in t:
            units = "IN"
        else:
            units = "MM"

    # threshold: first numeric
    m = re.search(r"(-?\d+(\.\d+)?)", t)
    if not m:
        raise ValueError("No numeric threshold found")
    thr = float(m.group(1))

    return metric, thr, op, units


def open_meteo_hourly(lat: float, lon: float, tz: str) -> dict:
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,windspeed_10m",
        "timezone": tz,
    }
    url = base + "?" + urlencode(params)
    r = requests.get(url, timeout=HTTP_TIMEOUT, headers=UA)
    r.raise_for_status()
    return r.json()


def _c_to_f(x: float) -> float:
    return x * 9.0 / 5.0 + 32.0


def _mm_to_in(x: float) -> float:
    return x / 25.4


def compute_daily_metric(data: dict, date_iso: str, metric: str, units: str) -> float:
    times = data["hourly"]["time"]
    temp = data["hourly"]["temperature_2m"]
    precip = data["hourly"]["precipitation"]
    wind = data["hourly"]["windspeed_10m"]

    vals_temp, vals_precip, vals_wind = [], [], []
    for i, ts in enumerate(times):
        if ts.startswith(date_iso):
            vals_temp.append(float(temp[i]))
            vals_precip.append(float(precip[i]))
            vals_wind.append(float(wind[i]))

    if not vals_temp:
        raise ValueError("No hourly data for date (timezone/date mismatch)")

    if metric == "tmax":
        v = max(vals_temp)
        if units == "F":
            v = _c_to_f(v)
        return float(v)

    if metric == "tmin":
        v = min(vals_temp)
        if units == "F":
            v = _c_to_f(v)
        return float(v)

    if metric == "precip":
        v = sum(vals_precip)  # mm by default
        if units == "IN":
            v = _mm_to_in(v)
        return float(v)

    if metric == "windmax":
        v = max(vals_wind)  # km/h usually from Open-Meteo; we keep numeric as-is
        return float(v)

    v = max(vals_temp)
    if units == "F":
        v = _c_to_f(v)
    return float(v)


def _norm_cdf(z: float) -> float:
    # Normal CDF using erf; good enough for trading decisions
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_yes_from_point_forecast(point: float, threshold: float, op: str, metric: str, units: str) -> float:
    """
    Convert point forecast -> probability using a conservative uncertainty model.
    This is NOT perfect, but it turns weather into a tradable probability.
    """
    # sigma defaults by metric
    if metric in ["tmax", "tmin"]:
        sigma = WEATHER_SIGMA_C
        if units == "F":
            sigma = WEATHER_SIGMA_C * 9.0 / 5.0
    elif metric == "precip":
        sigma = 2.5 if units == "MM" else 0.10  # ~2.5mm or ~0.1 inch
    elif metric == "windmax":
        sigma = 4.0  # km/h-ish uncertainty
    else:
        sigma = WEATHER_SIGMA_C

    sigma = max(0.2, float(sigma))

    # If op is >= : P = P(X >= thr)
    if op == ">=":
        z = (point - threshold) / sigma
        return float(_clamp(1.0 - _norm_cdf(-z), 0.0, 1.0))  # same as CDF(z)
    else:
        z = (threshold - point) / sigma
        return float(_clamp(1.0 - _norm_cdf(-z), 0.0, 1.0))


def recommend_from_prob(p_yes: float, market_yes: float, edge_min: float) -> Tuple[str, str, float]:
    edge = p_yes - market_yes
    if edge >= edge_min:
        return ("ENTER YES (A FAVOR)", f"P(YES)={p_yes:.0%} vs Market={market_yes:.0%} | Edge=+{edge:.0%}", edge)
    if (-edge) >= edge_min:
        return ("ENTER NO (CONTRA)", f"P(YES)={p_yes:.0%} vs Market={market_yes:.0%} | Edge={edge:.0%} (YES overpriced)", edge)
    return ("WAIT / WATCH", f"P(YES)={p_yes:.0%} vs Market={market_yes:.0%} | Edge={edge:.0%} (small)", edge)


def weather_edge_decision(title: str, yes_price: float) -> Optional[Dict[str, Any]]:
    city = _parse_city(title)
    if not city:
        return None

    date_iso = _parse_date(title)
    metric, threshold, op, units = _parse_metric_threshold(title)

    data = open_meteo_hourly(city["lat"], city["lon"], city["tz"])
    point = compute_daily_metric(data, date_iso, metric, units)

    p_yes = prob_yes_from_point_forecast(point, threshold, op, metric, units)
    action, why, edge = recommend_from_prob(p_yes, yes_price, WEATHER_EDGE_MIN)

    # Build a compact explanation
    metric_label = {"tmax": "Temp MAX", "tmin": "Temp MIN", "precip": "Precip", "windmax": "Wind MAX"}.get(metric, metric)
    cond = f"{metric_label} {op} {threshold:g}{('°'+units) if units in ['C','F'] else (' '+units)}"
    return {
        "city": city["name"],
        "date": date_iso,
        "cond": cond,
        "forecast_point": point,
        "units": units,
        "p_yes": p_yes,
        "edge": edge,
        "action": action,
        "why": why,
    }


# ----------------------------
# Fetch Polymarket markets
# ----------------------------
def fetch_markets() -> List[Dict[str, Any]]:
    if POLY_ENDPOINT:
        r = requests.get(POLY_ENDPOINT, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
            return data["markets"]
        if isinstance(data, list):
            return data
        return []

    url = "https://gamma-api.polymarket.com/markets"
    out: List[Dict[str, Any]] = []
    limit = min(MAX_MARKETS, 200)
    offset = 0

    while len(out) < MAX_MARKETS:
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(limit),
            "offset": str(offset),
            "order": "volume",
            "ascending": "false",
        }
        r = requests.get(url, params=params, headers=UA, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        chunk = r.json()
        if not isinstance(chunk, list) or not chunk:
            break
        out.extend(chunk)
        if len(chunk) < limit:
            break
        offset += limit
        if offset > 1000:
            break

    return out[:MAX_MARKETS]


# ----------------------------
# General recommendation (non-weather)
# ----------------------------
def recommend_general(yes_now: float, yes_prev: Optional[float]) -> Tuple[str, str]:
    if yes_prev is None:
        # Always explicit even without baseline
        if yes_now <= 0.35:
            return ("ENTER YES (A FAVOR)", "YES is cheap (value side)")
        if yes_now >= 0.65:
            return ("ENTER NO (CONTRA)", "YES is expensive (value on NO)")
        return ("WAIT / WATCH", "no baseline yet; mid-price zone")
    d = yes_now - yes_prev
    if abs(d) < 0.002:
        return ("WAIT / WATCH", "flat since last scan")
    if d > 0:
        return ("ENTER YES (A FAVOR)", f"momentum up (+{d:.3f})")
    return ("ENTER NO (CONTRA)", f"momentum down ({d:.3f})")


# ----------------------------
# Main loop
# ----------------------------
def main() -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return

    last_sent_ts: Dict[str, float] = {}
    last_yes: Dict[str, float] = {}
    last_vol: Dict[str, float] = {}

    send_telegram("🤖 Bot ON: AGGRESSIVE_EDGE v3 (London/Buenos Aires/Ankara weather edge + explicit YES/NO + flow move%).")

    while True:
        try:
            markets = fetch_markets()
        except Exception as e:
            now = time.time()
            if now - last_sent_ts.get("FETCH_ERROR", 0) > 1200:
                send_telegram(f"⚠️ Fetch error: {type(e).__name__}: {e}")
                last_sent_ts["FETCH_ERROR"] = now
            time.sleep(SCAN_EVERY_SEC)
            continue

        now = time.time()
        alerts_sent = 0

        for m in markets:
            try:
                key = _market_key(m)
                title = _title(m)
                url = _url(m)

                yes = _best_yes_price(m)
                if yes is None:
                    continue
                yes = _clamp(yes, 0.0, 1.0)

                bid, ask = _best_bid_ask(m)
                spread = _spread(m)
                vol = _volume(m)
                liq = _liquidity(m)

                # Aggressive but sane filters
                if vol < VOL_MIN and liq < LIQ_MIN:
                    continue

                prev_yes = last_yes.get(key)
                prev_vol = last_vol.get(key, vol)

                # Price move (%)
                move_abs = abs(yes - prev_yes) if prev_yes is not None else 0.0
                price_move_pct = (abs(yes - prev_yes) / max(prev_yes, 1e-9)) if prev_yes is not None else 0.0

                # Money flow move (%): volume delta %
                vol_delta = vol - prev_vol
                vol_move_pct = (abs(vol_delta) / max(prev_vol, 1e-9)) if prev_vol is not None else 0.0

                score = _score(vol=vol, liq=liq, spread=spread, move_abs=move_abs)

                # Gate: allow alerts if any of these is strong
                strong_enough = (
                    score >= SCORE_MIN
                    or spread >= GAP_MIN
                    or (prev_yes is not None and price_move_pct >= (REARM_PRICE_MOVE_PCT / 100.0))
                    or (prev_vol is not None and vol_move_pct >= (REARM_VOL_MOVE_PCT / 100.0))
                )
                if not strong_enough:
                    last_yes[key] = yes
                    last_vol[key] = vol
                    continue

                # Cooldown + rearm
                t_last = last_sent_ts.get(key, 0.0)
                cooldown_ok = (now - t_last) >= COOLDOWN_SEC
                rearm_ok = (
                    (prev_yes is not None and price_move_pct >= (REARM_PRICE_MOVE_PCT / 100.0))
                    or (prev_vol is not None and vol_move_pct >= (REARM_VOL_MOVE_PCT / 100.0))
                )
                if not cooldown_ok and not rearm_ok:
                    last_yes[key] = yes
                    last_vol[key] = vol
                    continue

                # WEATHER EDGE path (only for your cities)
                weather_info = None
                if is_weather_market(title):
                    try:
                        weather_info = weather_edge_decision(title, yes)
                    except Exception:
                        weather_info = None

                tier = _tier(score=score, spread=spread, move_abs=move_abs)

                # Recommendation
                if weather_info:
                    action = weather_info["action"]
                    why = weather_info["why"]
                    extra_line = (
                        f"🌦 WeatherEdge: {weather_info['city']} | {weather_info['date']}\n"
                        f"🔎 Rule: {weather_info['cond']} | Forecast≈{weather_info['forecast_point']:.1f}"
                        f"{('°'+weather_info['units']) if weather_info['units'] in ['C','F'] else ' '+weather_info['units']}"
                    )
                else:
                    action, why = recommend_general(yes, prev_yes)
                    extra_line = ""

                no = _clamp(1.0 - yes, 0.0, 1.0)

                spread_cents = spread * 100.0
                bid_s = f"{bid:.3f}" if bid is not None else "n/a"
                ask_s = f"{ask:.3f}" if ask is not None else "n/a"

                # Make FLOW MOVE% explicit (money in/out)
                flow_dir = "IN" if vol_delta >= 0 else "OUT"
                flow_amt = abs(vol_delta)

                msg = (
                    f"🚨 {tier} | ALERT\n"
                    f"✅ RECOMMENDATION: {action}\n"
                    f"🧠 Reason: {why}\n"
                    f"💰 YES={yes:.3f} | NO≈{no:.3f} | PriceMove={_pct(price_move_pct)}\n"
                    f"💸 FlowMove: {flow_dir} {flow_amt:,.0f} | FlowMove%={_pct(vol_move_pct)}  (Vol {prev_vol:,.0f}→{vol:,.0f})\n"
                    f"📌 Spread: {spread_cents:.2f}¢ ({_pct(spread)}) [bid={bid_s} / ask={ask_s}]\n"
                    f"📊 Liq={liq:,.0f} | Score={score:.2f}\n"
                    f"{extra_line + chr(10) if extra_line else ''}"
                    f"📝 {title}\n"
                    f"{url}"
                )

                send_telegram(msg)
                last_sent_ts[key] = now
                alerts_sent += 1

                # cap per scan (aggressive but not insane)
                if alerts_sent >= 16:
                    break

                last_yes[key] = yes
                last_vol[key] = vol

            except Exception:
                continue

        time.sleep(SCAN_EVERY_SEC)


if __name__ == "__main__":
    main()
