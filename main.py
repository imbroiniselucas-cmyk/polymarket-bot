import os
import time
import json
import hashlib
import traceback
from typing import Any, Dict, List, Optional, Tuple
import requests

# =========================
# CONFIG (env vars)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Your data endpoint that returns JSON
DATA_URL = os.getenv("DATA_URL", "").strip()

# Polling
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "300"))  # 300=5min

# Heartbeat
HEARTBEAT_MINUTES = int(os.getenv("HEARTBEAT_MINUTES", "30"))

# Filters (informational)
MAX_SPREAD = float(os.getenv("MAX_SPREAD", "0.05"))        # 5%
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "1000"))  # adjust
MIN_VOL_24H = float(os.getenv("MIN_VOL_24H", "500"))       # adjust
MIN_MOVE = float(os.getenv("MIN_MOVE", "0.03"))            # 3% move vs last seen

# Anti-spam
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "3600"))  # 1h
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "8"))

HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))

# =========================
# TELEGRAM
# =========================
def tg_send(text: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()

# =========================
# FETCH + PARSE (ADAPT parse_items ONLY)
# =========================
def fetch_json() -> Any:
    if not DATA_URL:
        raise RuntimeError("DATA_URL is empty. Set it in your environment variables.")
    r = requests.get(DATA_URL, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()

def parse_items(raw: Any) -> List[Dict[str, Any]]:
    """
    Adapt this to your JSON format.
    We output a list of items with:
      id (str) required
      title (str) optional
      url (str) optional
      bid (float) optional
      ask (float) optional
      last_price (float) optional
      liquidity (float) optional
      vol24h (float) optional
    """
    items: List[Dict[str, Any]] = []

    # Common patterns: {"items":[...]} or {"markets":[...]} or a list directly
    if isinstance(raw, dict):
        data = raw.get("items") or raw.get("markets") or raw.get("data") or raw.get("results")
    else:
        data = raw

    if not isinstance(data, list):
        return items

    for x in data:
        if not isinstance(x, dict):
            continue

        _id = str(x.get("id") or x.get("slug") or x.get("symbol") or x.get("market_id") or "")
        if not _id:
            continue

        title = str(x.get("title") or x.get("name") or x.get("question") or _id)
        url = str(x.get("url") or x.get("link") or "")

        # Try to find bid/ask in common keys
        bid = x.get("bid") or x.get("bestBid") or x.get("best_bid")
        ask = x.get("ask") or x.get("bestAsk") or x.get("best_ask")

        last_price = x.get("last_price") or x.get("lastPrice") or x.get("price") or x.get("mid")

        liquidity = x.get("liquidity") or x.get("liquidityUSD") or x.get("liquidity_usd")
        vol24h = x.get("vol24h") or x.get("volume24h") or x.get("volume_24h")

        def to_float(v) -> Optional[float]:
            try:
                return float(v) if v is not None else None
            except Exception:
                return None

        items.append({
            "id": _id,
            "title": title,
            "url": url,
            "bid": to_float(bid),
            "ask": to_float(ask),
            "last_price": to_float(last_price),
            "liquidity": to_float(liquidity),
            "vol24h": to_float(vol24h),
        })

    return items

# =========================
# METRICS
# =========================
def compute_spread(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid  # e.g. 0.03 = 3%

def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / abs(old)

def fingerprint(it: Dict[str, Any]) -> str:
    payload = {
        "id": it.get("id"),
        "bid": it.get("bid"),
        "ask": it.get("ask"),
        "last_price": it.get("last_price"),
        "liquidity": it.get("liquidity"),
        "vol24h": it.get("vol24h"),
    }
    s = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

# =========================
# ALERT RULES (informational)
# =========================
def qualifies(it: Dict[str, Any], last_seen_price: Optional[float]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []

    sp = compute_spread(it.get("bid"), it.get("ask"))
    liq = it.get("liquidity")
    vol = it.get("vol24h")
    lp = it.get("last_price")

    if sp is None:
        reasons.append("no spread (missing bid/ask)")
        ok_spread = False
    else:
        ok_spread = sp <= MAX_SPREAD
        if not ok_spread:
            reasons.append(f"spread too high {sp:.2%} > {MAX_SPREAD:.2%}")

    ok_liq = True
    if liq is not None and liq < MIN_LIQUIDITY:
        ok_liq = False
        reasons.append(f"low liquidity {liq:.0f} < {MIN_LIQUIDITY:.0f}")

    ok_vol = True
    if vol is not None and vol < MIN_VOL_24H:
        ok_vol = False
        reasons.append(f"low vol24h {vol:.0f} < {MIN_VOL_24H:.0f}")

    move = pct_change(lp, last_seen_price)
    ok_move = True
    if move is not None and abs(move) < MIN_MOVE:
        ok_move = False
        reasons.append(f"small move {move:.2%} < {MIN_MOVE:.2%}")

    # We want "interesting but exit-friendly": spread must pass.
    ok = ok_spread and ok_liq and ok_vol and ok_move
    return ok, reasons

def format_alert(it: Dict[str, Any], last_seen_price: Optional[float]) -> str:
    title = it.get("title") or it["id"]
    url = it.get("url") or ""
    bid = it.get("bid")
    ask = it.get("ask")
    lp = it.get("last_price")
    liq = it.get("liquidity")
    vol = it.get("vol24h")

    sp = compute_spread(bid, ask)
    move = pct_change(lp, last_seen_price)

    lines = [f"📈 Monitor alert: {title}"]
    if url:
        lines.append(url)

    if lp is not None:
        lines.append(f"Last: {lp:.6f}")
    if bid is not None and ask is not None:
        lines.append(f"Bid/Ask: {bid:.6f} / {ask:.6f}")
    if sp is not None:
        lines.append(f"Spread: {sp:.2%} (max {MAX_SPREAD:.2%})")
    if move is not None:
        lines.append(f"Move vs last seen: {move:+.2%} (min |{MIN_MOVE:.2%}|)")
    if liq is not None:
        lines.append(f"Liquidity: {liq:.0f} (min {MIN_LIQUIDITY:.0f})")
    if vol is not None:
        lines.append(f"Vol 24h: {vol:.0f} (min {MIN_VOL_24H:.0f})")

    lines.append("Info-only alert (no action recommendation).")
    return "\n".join(lines)

# =========================
# STATE
# =========================
_last_heartbeat = 0.0
_last_alert_ts: Dict[str, float] = {}
_last_fp: Dict[str, str] = {}
_last_price_seen: Dict[str, float] = {}

def heartbeat() -> None:
    global _last_heartbeat
    now = time.time()
    if _last_heartbeat == 0 or (now - _last_heartbeat) >= HEARTBEAT_MINUTES * 60:
        _last_heartbeat = now
        tg_send(
            "✅ Bot online.\n"
            f"Poll={POLL_SECONDS}s | maxSpread={MAX_SPREAD:.2%} | "
            f"minLiq={MIN_LIQUIDITY:.0f} | minVol24h={MIN_VOL_24H:.0f} | "
            f"minMove={MIN_MOVE:.2%}"
        )

def should_send(item_id: str, fp: str) -> bool:
    now = time.time()
    last_ts = _last_alert_ts.get(item_id, 0.0)
    last_fp = _last_fp.get(item_id)
    changed = (last_fp != fp)
    cooldown_ok = (now - last_ts) >= ALERT_COOLDOWN_SECONDS
    return changed and cooldown_ok

def run_cycle() -> None:
    raw = fetch_json()
    items = parse_items(raw)

    sent = 0
    for it in items:
        item_id = it["id"]
        prev_price = _last_price_seen.get(item_id)

        ok, _reasons = qualifies(it, prev_price)
        fp = fingerprint(it)

        # update last seen price no matter what
        if it.get("last_price") is not None:
            _last_price_seen[item_id] = float(it["last_price"])

        if not ok:
            continue

        if should_send(item_id, fp):
            tg_send(format_alert(it, prev_price))
            _last_alert_ts[item_id] = time.time()
            _last_fp[item_id] = fp
            sent += 1
            if sent >= MAX_ALERTS_PER_CYCLE:
                break

def main() -> None:
    tg_send("🚀 Starting bot...")
    while True:
        try:
            heartbeat()
            run_cycle()
        except Exception as e:
            err = "".join(traceback.format_exception(type(e), e, e.__traceback__))
            msg = "❌ Bot error:\n" + err[-1500:]
            try:
                tg_send(msg)
            except Exception:
                print(msg)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
