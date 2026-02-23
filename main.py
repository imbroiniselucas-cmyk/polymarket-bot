import os
import json
import time
import math
import requests
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

GAMMA = "https://gamma-api.polymarket.com"  # market discovery / tags :contentReference[oaicite:4]{index=4}
CLOB = "https://clob.polymarket.com"        # orderbooks / spreads :contentReference[oaicite:5]{index=5}

# -----------------------------
# CONFIG
# -----------------------------
CONFIG = {
    # Tags you care about. The bot will try to find these tags in /tags (by slug or name).
    # If it can't find them, it will fallback to keyword/category filtering.
    "target_tags": [
        "politics",
        "climate",
        "weather",
        "elections",
    ],

    # Additional keyword filters (question/description/category) to keep only politics + climate-ish markets
    "include_keywords": [
        "election", "president", "prime minister", "parliament", "poll",
        "storm", "hurricane", "rain", "snow", "temperature", "heat", "cold",
        "climate", "weather", "forecast", "wind", "flood",
        "EU", "US", "UK", "Germany", "Netherlands"
    ],
    "exclude_keywords": [
        "sports", "nba", "nfl", "mlb", "champions league"
    ],

    # Liquidity/volume guardrails (Gamma returns liquidityNum/volume24hr fields)
    "min_liquidity": 1000.0,
    "min_volume_24h": 500.0,

    # Spread guardrail (if spread too wide, it's hard to exit)
    "max_spread": 0.08,  # 8 cents

    # Polling interval
    "poll_seconds": 40,

    # How many markets to scan each cycle (to avoid rate limit)
    "max_markets_per_cycle": 120,

    # Arbitrage groups (YOU DEFINE THESE).
    # Each group is a set of market slugs that represent mutually exclusive outcomes.
    # Arbitrage condition: sum(best_ask_yes) < 1 - edge
    "arb_groups": [
        # Example (replace with real Polymarket market slugs):
        # {
        #   "name": "US election winner set",
        #   "slugs": ["candidate-a-wins-2026", "candidate-b-wins-2026", "candidate-c-wins-2026"],
        #   "edge": 0.01  # require at least 1% theoretical edge
        # }
    ],

    # Exit rule (simple): if midpoint moved up by X% from last seen and spread still acceptable -> alert exit
    "exit_takeprofit_pct": 0.20,  # 20%

    # News
    "news_enabled": True,
    "news_max_items": 3,
    "news_lookback_hours": 24,

    # Telegram alerts (optional)
    "telegram": {
        "enabled": False,
        "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
    }
}

# -----------------------------
# Utilities
# -----------------------------
def http_get(url: str, params: Optional[dict] = None, timeout: int = 20) -> Any:
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def safe_lower(x: Any) -> str:
    return (str(x) if x is not None else "").lower()

def contains_any(text: str, needles: List[str]) -> bool:
    t = safe_lower(text)
    return any(n.lower() in t for n in needles)

def parse_listish(value: Any) -> List[str]:
    """
    Polymarket sometimes returns arrays as JSON strings (e.g., '["id1","id2"]').
    Handle list, stringified list, or empty.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    return [str(v) for v in arr]
            except Exception:
                pass
        # If it's a single token id
        return [s]
    return []

def fmt(x: Optional[float]) -> str:
    if x is None or math.isnan(x):
        return "-"
    return f"{x:.4f}"

# -----------------------------
# Polymarket: Tags + Markets
# -----------------------------
def fetch_tags() -> List[Dict[str, Any]]:
    # Gamma tags endpoint is public :contentReference[oaicite:6]{index=6}
    return http_get(f"{GAMMA}/tags", params={"limit": 500})

def find_tag_ids(tags: List[Dict[str, Any]], wanted: List[str]) -> List[int]:
    wanted_l = [w.lower() for w in wanted]
    ids: List[int] = []
    for t in tags:
        slug = safe_lower(t.get("slug"))
        name = safe_lower(t.get("name"))
        if any(w == slug or w == name for w in wanted_l):
            try:
                ids.append(int(t["id"]))
            except Exception:
                pass
    # de-dup
    return sorted(list(set(ids)))

def fetch_markets_by_tag(tag_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    # Best practice: filter by tag_id + active=true + closed=false :contentReference[oaicite:7]{index=7}
    markets: List[Dict[str, Any]] = []
    offset = 0
    while True:
        batch = http_get(
            f"{GAMMA}/markets",
            params={
                "tag_id": tag_id,
                "active": "true",
                "closed": "false",
                "limit": limit,
                "offset": offset,
            },
        )
        if not isinstance(batch, list) or len(batch) == 0:
            break
        markets.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        if offset >= 500:  # safety cap per tag
            break
    return markets

def fallback_fetch_active_markets(limit: int = 200) -> List[Dict[str, Any]]:
    # If tags fail, fetch some active markets and keyword-filter :contentReference[oaicite:8]{index=8}
    return http_get(
        f"{GAMMA}/markets",
        params={"active": "true", "closed": "false", "limit": limit, "offset": 0},
    )

# -----------------------------
# Polymarket: Orderbook/Spread
# -----------------------------
@dataclass
class BookTop:
    bid: Optional[float]
    ask: Optional[float]
    spread: Optional[float]
    midpoint: Optional[float]

def fetch_book_top(token_id: str) -> BookTop:
    # CLOB orderbook endpoint: GET /book :contentReference[oaicite:9]{index=9}
    data = http_get(f"{CLOB}/book", params={"token_id": token_id})
    bids = data.get("bids", []) or []
    asks = data.get("asks", []) or []

    def best_price(levels: list) -> Optional[float]:
        # levels are typically [{"price":"0.52","size":"10"}, ...]
        if not levels:
            return None
        try:
            return float(levels[0]["price"])
        except Exception:
            return None

    bid = best_price(bids)
    ask = best_price(asks)
    spread = (ask - bid) if (bid is not None and ask is not None) else None
    midpoint = ((ask + bid) / 2.0) if (bid is not None and ask is not None) else None
    return BookTop(bid=bid, ask=ask, spread=spread, midpoint=midpoint)

# -----------------------------
# News (GDELT DOC API)
# -----------------------------
def gdelt_news(query: str, lookback_hours: int, max_items: int) -> List[Dict[str, str]]:
    # Simple GDELT DOC 2.0 query (public) :contentReference[oaicite:10]{index=10}
    # Note: GDELT params are picky; this is a pragmatic "good enough" setup.
    # We keep it short: recent, English-ish sources.
    start = int(time.time()) - lookback_hours * 3600
    # GDELT uses "mode=ArtList" for list results.
    url = "https://api.gdeltproject.org/api/v2/doc/doc"
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max_items,
        "startdatetime": time.strftime("%Y%m%d%H%M%S", time.gmtime(start)),
        "sort": "HybridRel",
    }
    try:
        data = http_get(url, params=params, timeout=25)
        arts = data.get("articles", []) or []
        out = []
        for a in arts[:max_items]:
            out.append({
                "title": a.get("title", "")[:180],
                "url": a.get("url", ""),
                "source": a.get("sourceCountry", "") or a.get("sourceCommonName", "")
            })
        return out
    except Exception:
        return []

# -----------------------------
# Filtering + Scanning
# -----------------------------
def market_passes_filters(m: Dict[str, Any]) -> bool:
    q = safe_lower(m.get("question"))
    desc = safe_lower(m.get("description"))
    cat = safe_lower(m.get("category"))

    if contains_any(q + " " + desc + " " + cat, CONFIG["exclude_keywords"]):
        return False
    if not contains_any(q + " " + desc + " " + cat, CONFIG["include_keywords"]):
        return False

    liq = float(m.get("liquidityNum") or 0)
    vol = float(m.get("volume24hr") or 0)
    if liq < CONFIG["min_liquidity"]:
        return False
    if vol < CONFIG["min_volume_24h"]:
        return False

    if not m.get("enableOrderBook", False):
        return False

    return True

def extract_yes_token_id(m: Dict[str, Any]) -> Optional[str]:
    # Docs: clobTokenIds -> [Yes, No] :contentReference[oaicite:11]{index=11}
    ids = parse_listish(m.get("clobTokenIds"))
    return ids[0] if len(ids) >= 1 else None

def send_telegram(text: str) -> None:
    tg = CONFIG["telegram"]
    if not tg["enabled"]:
        return
    if not tg["bot_token"] or not tg["chat_id"]:
        return
    url = f"https://api.telegram.org/bot{tg['bot_token']}/sendMessage"
    try:
        requests.post(url, data={"chat_id": tg["chat_id"], "text": text}, timeout=20)
    except Exception:
        pass

# -----------------------------
# Arbitrage detection
# -----------------------------
def build_slug_map(markets: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mp = {}
    for m in markets:
        slug = m.get("slug")
        if slug:
            mp[str(slug)] = m
    return mp

def check_arb_groups(slug_map: Dict[str, Dict[str, Any]], tops: Dict[str, BookTop]) -> List[str]:
    alerts: List[str] = []
    for g in CONFIG["arb_groups"]:
        name = g.get("name", "arb-group")
        slugs = g.get("slugs", [])
        edge = float(g.get("edge", 0.0))
        asks: List[Tuple[str, float]] = []
        missing = []
        for s in slugs:
            if s not in slug_map:
                missing.append(s)
                continue
            top = tops.get(s)
            if not top or top.ask is None:
                missing.append(s)
                continue
            asks.append((s, top.ask))

        if missing or not asks:
            continue

        total = sum(a for _, a in asks)
        if total < (1.0 - edge):
            lines = [f"✅ ARB FOUND: {name}", f"Sum(best_ask_yes)={total:.4f}  (< {1.0-edge:.4f})"]
            for s, a in sorted(asks, key=lambda x: x[1]):
                lines.append(f"- {s}: ask={a:.4f}")
            alerts.append("\n".join(lines))
    return alerts

# -----------------------------
# Main loop
# -----------------------------
def main():
    print("Polymarket Climate+Politics Arb Scanner (read-only)")

    # 1) Get tag ids (best path) :contentReference[oaicite:12]{index=12}
    markets: List[Dict[str, Any]] = []
    try:
        tags = fetch_tags()
        tag_ids = find_tag_ids(tags, CONFIG["target_tags"])
    except Exception:
        tag_ids = []

    if tag_ids:
        for tid in tag_ids[:6]:
            try:
                markets.extend(fetch_markets_by_tag(tid, limit=100))
            except Exception:
                pass
    else:
        # Fallback: scan a slice and keyword-filter :contentReference[oaicite:13]{index=13}
        try:
            markets = fallback_fetch_active_markets(limit=250)
        except Exception:
            markets = []

    # de-dup by id
    seen = set()
    uniq = []
    for m in markets:
        mid = m.get("id")
        if mid and mid not in seen:
            uniq.append(m)
            seen.add(mid)
    markets = uniq

    # keep only markets that match your criteria
    markets = [m for m in markets if market_passes_filters(m)]
    markets = sorted(markets, key=lambda x: float(x.get("volume24hr") or 0), reverse=True)
    print(f"Discovered {len(markets)} filtered markets (climate/politics-ish)")

    # state for exit/takeprofit signals
    last_mid: Dict[str, float] = {}

    while True:
        cycle = markets[: CONFIG["max_markets_per_cycle"]]
        tops_by_slug: Dict[str, BookTop] = {}

        for m in cycle:
            slug = str(m.get("slug") or "")
            yes_id = extract_yes_token_id(m)
            if not slug or not yes_id:
                continue

            try:
                top = fetch_book_top(yes_id)
            except Exception:
                continue

            # Spread filter
            if top.spread is not None and top.spread > CONFIG["max_spread"]:
                continue

            tops_by_slug[slug] = top

            # Exit/takeprofit signal (simple)
            if top.midpoint is not None:
                prev = last_mid.get(slug)
                if prev is not None and prev > 0:
                    change = (top.midpoint - prev) / prev
                    if change >= CONFIG["exit_takeprofit_pct"] and (top.bid is not None):
                        msg = (
                            f"📈 EXIT SIGNAL: {slug}\n"
                            f"midpoint {prev:.4f} -> {top.midpoint:.4f}  (+{change*100:.1f}%)\n"
                            f"bid={fmt(top.bid)} ask={fmt(top.ask)} spread={fmt(top.spread)}"
                        )
                        print(msg)
                        send_telegram(msg)
                last_mid[slug] = top.midpoint

        # Arbitrage groups (portfolio arb)
        slug_map = build_slug_map(cycle)
        arb_alerts = check_arb_groups(slug_map, tops_by_slug)
        for a in arb_alerts:
            print(a)
            send_telegram(a)

        # News block (top markets by volume)
        if CONFIG["news_enabled"]:
            top_slugs = list(tops_by_slug.keys())[:8]
            for s in top_slugs[:3]:
                q = slug_map.get(s, {}).get("question", s)
                arts = gdelt_news(q, CONFIG["news_lookback_hours"], CONFIG["news_max_items"])
                if arts:
                    print(f"\n📰 NEWS for: {s}")
                    for it in arts:
                        print(f"- {it['title']} | {it['url']}")

        print(f"\n--- sleeping {CONFIG['poll_seconds']}s ---\n")
        time.sleep(CONFIG["poll_seconds"])

if __name__ == "__main__":
    # Ensure requests exists. If you got ModuleNotFoundError before:
    # pip install requests
    main()
