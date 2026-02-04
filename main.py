#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Polymarket Layered Bot (Scanner -> Intel -> Decision -> PaperTrade/Alerts)
More proactive (slightly more aggressive defaults) while keeping anti-spam + insights.

- Dependencies: requests (plus stdlib)
- Deploy: Railway (cron runs MODE=SCANNER hourly/90min recommended)
- Default: DRY_RUN=1 (paper trades only)

ENV (key ones):
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
MODE=SCANNER|MIDDAY|REPORT|NIGHT
DRY_RUN=1
DB_PATH=bot.db
"""

import os
import sys
import time
import json
import math
import sqlite3
import traceback
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple

import requests

# =========================
# CONFIG (ENV VARS)
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MODE = os.getenv("MODE", "SCANNER").strip().upper()  # SCANNER | MIDDAY | REPORT | NIGHT
DRY_RUN = os.getenv("DRY_RUN", "1").strip()  # "1" => paper-trade only
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()

# Polymarket APIs (public)
GAMMA_BASE = os.getenv("GAMMA_BASE", "https://gamma-api.polymarket.com").strip()

# Scanner scope (more aggressive defaults)
ACTIVE_ONLY = os.getenv("ACTIVE_ONLY", "1").strip() == "1"
MAX_MARKETS_FETCH = int(os.getenv("MAX_MARKETS_FETCH", "900").strip())
CANDIDATES_TOP_N = int(os.getenv("CANDIDATES_TOP_N", "22").strip())
MIN_SCORE = float(os.getenv("MIN_SCORE", "6.8").strip())

# Filters (more aggressive defaults)
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "10000").strip())
MIN_VOL_DELTA = float(os.getenv("MIN_VOL_DELTA", "150").strip())
MIN_PRICE_MOVE_PCT = float(os.getenv("MIN_PRICE_MOVE_PCT", "0.6").strip())

EDGE_MIN_PP = float(os.getenv("EDGE_MIN_PP", "3.2").strip())  # min edge in percentage points
CONF_MIN = float(os.getenv("CONF_MIN", "0.55").strip())

# Anti-spam / cooldown (slightly more aggressive)
ALERT_COOLDOWN_MIN = int(os.getenv("ALERT_COOLDOWN_MIN", "35").strip())
REALERT_EDGE_BUMP_PP = float(os.getenv("REALERT_EDGE_BUMP_PP", "0.9").strip())

# Alert limits
MAX_ALERTS_PER_RUN = int(os.getenv("MAX_ALERTS_PER_RUN", "6").strip())

# Intel (optional): GDELT news pulse
USE_GDELT = os.getenv("USE_GDELT", "1").strip() == "1"
GDELT_WINDOW_H = int(os.getenv("GDELT_WINDOW_H", "24").strip())

# Proactive digest
SEND_WATCHLIST_IF_NO_ALERTS = os.getenv("SEND_WATCHLIST_IF_NO_ALERTS", "1").strip() == "1"
WATCHLIST_TOP_K = int(os.getenv("WATCHLIST_TOP_K", "4").strip())

# Paper trading
PAPER_EXIT_HOURS = float(os.getenv("PAPER_EXIT_HOURS", "6").strip())
PAPER_STAKE_USD = float(os.getenv("PAPER_STAKE_USD", "10").strip())

# HTTP
TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12").strip())
UA = os.getenv("HTTP_UA", "Mozilla/5.0 (compatible; PolymarketLayerBot/1.2)")

# =========================
# UTIL
# =========================
def ts() -> int:
    return int(time.time())

def clamp(x: float, a: float, b: float) -> float:
    return max(a, min(b, x))

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def safe_int(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(float(x))
    except Exception:
        return default

def http_get(url: str, params: Optional[Dict[str, Any]] = None) -> Any:
    r = requests.get(url, params=params, timeout=TIMEOUT, headers={"User-Agent": UA})
    r.raise_for_status()
    ct = r.headers.get("content-type", "")
    if "application/json" in ct or "json" in ct:
        return r.json()
    return r.text

# =========================
# TELEGRAM (no dependency)
# =========================
def tg_send(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, json=payload, timeout=TIMEOUT).raise_for_status()
    except Exception:
        print("Telegram send failed. Printing instead:\n", text)

# =========================
# DB
# =========================
def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init() -> None:
    conn = db_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS markets_snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        market_id TEXT NOT NULL,
        slug TEXT,
        question TEXT,
        yes_price REAL,
        no_price REAL,
        liquidity REAL,
        volume REAL,
        end_time INTEGER,
        active INTEGER
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts_sent (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        market_id TEXT NOT NULL,
        slug TEXT,
        edge_pp REAL,
        score REAL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS paper_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at INTEGER NOT NULL,
        market_id TEXT NOT NULL,
        slug TEXT,
        side TEXT NOT NULL,
        entry_price REAL NOT NULL,
        stake_usd REAL NOT NULL,
        exit_at INTEGER,
        exit_price REAL,
        pnl_usd REAL,
        meta_json TEXT
    )
    """)

    conn.commit()
    conn.close()

def db_last_snapshot(market_id: str) -> Optional[sqlite3.Row]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM markets_snapshot
        WHERE market_id=?
        ORDER BY created_at DESC
        LIMIT 1
    """, (market_id,))
    row = cur.fetchone()
    conn.close()
    return row

def db_insert_snapshot(m: Dict[str, Any]) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO markets_snapshot
        (created_at, market_id, slug, question, yes_price, no_price, liquidity, volume, end_time, active)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        ts(),
        str(m.get("id", "")),
        m.get("slug"),
        m.get("question"),
        safe_float(m.get("yes_price")),
        safe_float(m.get("no_price")),
        safe_float(m.get("liquidity")),
        safe_float(m.get("volume")),
        safe_int(m.get("end_time")),
        1 if m.get("active") else 0
    ))
    conn.commit()
    conn.close()

def db_recent_alert(market_id: str) -> Optional[sqlite3.Row]:
    cutoff = ts() - ALERT_COOLDOWN_MIN * 60
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM alerts_sent
        WHERE market_id=? AND created_at>=?
        ORDER BY created_at DESC
        LIMIT 1
    """, (market_id, cutoff))
    row = cur.fetchone()
    conn.close()
    return row

def db_insert_alert(market_id: str, slug: str, edge_pp: float, score: float) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO alerts_sent (created_at, market_id, slug, edge_pp, score)
        VALUES (?,?,?,?,?)
    """, (ts(), market_id, slug, edge_pp, score))
    conn.commit()
    conn.close()

def db_open_paper_trades() -> List[sqlite3.Row]:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM paper_trades
        WHERE exit_at IS NULL
        ORDER BY created_at ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return rows

def db_create_paper_trade(market_id: str, slug: str, side: str, entry_price: float, stake_usd: float, meta: Dict[str, Any]) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO paper_trades (created_at, market_id, slug, side, entry_price, stake_usd, meta_json)
        VALUES (?,?,?,?,?,?,?)
    """, (ts(), market_id, slug, side, entry_price, stake_usd, json.dumps(meta, ensure_ascii=False)))
    conn.commit()
    conn.close()

def db_close_paper_trade(trade_id: int, exit_price: float, pnl_usd: float) -> None:
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE paper_trades
        SET exit_at=?, exit_price=?, pnl_usd=?
        WHERE id=?
    """, (ts(), exit_price, pnl_usd, trade_id))
    conn.commit()
    conn.close()

def db_paper_stats(days: int = 7) -> Dict[str, Any]:
    cutoff = ts() - days * 86400
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM paper_trades
        WHERE created_at>=? AND exit_at IS NOT NULL
        ORDER BY created_at ASC
    """, (cutoff,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return {"n": 0, "winrate": 0.0, "roi": 0.0, "pnl": 0.0, "avg_pnl": 0.0, "max_dd": 0.0}

    pnl_list = [safe_float(r["pnl_usd"]) for r in rows]
    stake_list = [safe_float(r["stake_usd"]) for r in rows]
    total_pnl = sum(pnl_list)
    total_stake = sum(stake_list) if sum(stake_list) > 0 else 1.0
    roi = total_pnl / total_stake

    wins = sum(1 for p in pnl_list if p > 0)
    winrate = wins / len(pnl_list)

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnl_list:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    return {
        "n": len(pnl_list),
        "winrate": winrate,
        "roi": roi,
        "pnl": total_pnl,
        "avg_pnl": total_pnl / len(pnl_list),
        "max_dd": max_dd
    }

# =========================
# POLYMARKET DATA FETCH
# =========================
def fetch_markets(limit: int = 200, active_only: bool = True) -> List[Dict[str, Any]]:
    params = {
        "limit": limit,
        "closed": "false",
        "active": "true" if active_only else "false",
        "order": "volume",
        "ascending": "false"
    }
    url = f"{GAMMA_BASE}/markets"
    data = http_get(url, params=params)

    if not isinstance(data, list):
        if isinstance(data, dict) and "markets" in data and isinstance(data["markets"], list):
            data = data["markets"]
        else:
            return []

    out: List[Dict[str, Any]] = []
    for m in data:
        q = m.get("question") or m.get("title") or m.get("name") or ""
        slug = m.get("slug") or ""
        mid = str(m.get("id") or m.get("market_id") or "")

        liquidity = safe_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidityUSD") or 0.0)
        volume = safe_float(m.get("volume") or m.get("volumeNum") or m.get("volumeUSD") or 0.0)

        yes_price = None
        no_price = None
        op = m.get("outcomePrices")
        if isinstance(op, list) and len(op) >= 2:
            yes_price = safe_float(op[0])
            no_price = safe_float(op[1])
        else:
            yes_price = safe_float(m.get("yesPrice"), 0.0)
            no_price = safe_float(m.get("noPrice"), 0.0)

        end_time = 0
        for k in ("endTime", "end_time", "closeTime", "closedTime", "expirationTime"):
            if m.get(k):
                try:
                    if isinstance(m.get(k), str) and "T" in m.get(k):
                        end_time = int(dt.datetime.fromisoformat(m[k].replace("Z", "+00:00")).timestamp())
                    else:
                        end_time = safe_int(m.get(k))
                except Exception:
                    end_time = 0
                break

        active = bool(m.get("active", True)) and not bool(m.get("closed", False))

        out.append({
            "id": mid,
            "slug": slug,
            "question": q.strip(),
            "yes_price": yes_price,
            "no_price": no_price,
            "liquidity": liquidity,
            "volume": volume,
            "end_time": end_time,
            "active": active,
            "url": f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"
        })
    return out

# =========================
# FEATURES / SCORE
# =========================
def compute_deltas(m: Dict[str, Any]) -> Dict[str, float]:
    last = db_last_snapshot(m["id"])
    if not last:
        return {"vol_delta": 0.0, "price_move_pct": 0.0}

    vol0 = safe_float(last["volume"])
    vol1 = safe_float(m.get("volume"))
    vol_delta = vol1 - vol0

    p0 = safe_float(last["yes_price"])
    p1 = safe_float(m.get("yes_price"))
    price_move_pct = 0.0
    if p0 > 0:
        price_move_pct = ((p1 - p0) / p0) * 100.0

    return {"vol_delta": vol_delta, "price_move_pct": price_move_pct}

def score_market(m: Dict[str, Any], deltas: Dict[str, float]) -> float:
    liq = safe_float(m.get("liquidity"))
    vol_delta = safe_float(deltas.get("vol_delta"))
    pm = abs(safe_float(deltas.get("price_move_pct")))

    # Liquidity score (0-5)
    liq_s = clamp(math.log10(liq + 1) - 3.0, 0.0, 5.0)

    # Volume delta score (0-5)
    vd_s = clamp(math.log10(max(vol_delta, 0) + 1) - 2.2, 0.0, 5.0)

    # Price move score (0-3)
    pm_s = clamp(pm / 1.8, 0.0, 3.0)

    # Urgency (0-2)
    urg_s = 0.6
    end_time = safe_int(m.get("end_time"))
    if end_time > 0:
        hours_left = (end_time - ts()) / 3600.0
        if hours_left <= 18:
            urg_s = 2.0
        elif hours_left <= 48:
            urg_s = 1.2
        elif hours_left <= 96:
            urg_s = 0.8
        else:
            urg_s = 0.6

    score = liq_s + vd_s + pm_s + urg_s
    return clamp(score, 0.0, 15.0)

def pick_candidates(markets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    for m in markets:
        deltas = compute_deltas(m)
        sc = score_market(m, deltas)

        liq = safe_float(m.get("liquidity"))
        if liq < MIN_LIQUIDITY:
            continue

        vol_delta = safe_float(deltas.get("vol_delta"))
        pm = abs(safe_float(deltas.get("price_move_pct")))

        # If we don't yet have deltas, allow big existing volume/liquidity
        if vol_delta < MIN_VOL_DELTA and pm < MIN_PRICE_MOVE_PCT and safe_float(m.get("volume")) < 75000:
            continue

        if sc < MIN_SCORE:
            continue

        m2 = dict(m)
        m2["_deltas"] = deltas
        m2["_score"] = sc
        scored.append(m2)

    scored.sort(key=lambda x: safe_float(x.get("_score")), reverse=True)
    return scored[:CANDIDATES_TOP_N]

# =========================
# INTEL (GDELT pulse)
# =========================
def gdelt_pulse(query: str, hours: int = 24) -> Dict[str, Any]:
    if not USE_GDELT or not query:
        return {"ok": False}

    def fmt(t: dt.datetime) -> str:
        return t.strftime("%Y%m%d%H%M%S")

    now = dt.datetime.utcnow()
    recent_start = now - dt.timedelta(hours=hours)
    prior_start = now - dt.timedelta(hours=2 * hours)
    prior_end = recent_start

    base = "https://api.gdeltproject.org/api/v2/doc/doc"
    common = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": 60,
        "sort": "HybridRel"
    }

    try:
        recent = http_get(base, params={**common, "startdatetime": fmt(recent_start), "enddatetime": fmt(now)})
        prior = http_get(base, params={**common, "startdatetime": fmt(prior_start), "enddatetime": fmt(prior_end)})

        rc = safe_int(recent.get("totalArticles"), 0) if isinstance(recent, dict) else 0
        pc = safe_int(prior.get("totalArticles"), 0) if isinstance(prior, dict) else 0

        sample = []
        if isinstance(recent, dict):
            arts = recent.get("articles") or []
            for a in arts[:4]:
                title = (a.get("title") or "").strip()
                url = (a.get("url") or "").strip()
                if title and url:
                    sample.append({"title": title, "url": url})

        spike = 0.0
        if pc > 0:
            spike = (rc - pc) / pc
        elif rc > 0:
            spike = 1.0

        return {"ok": True, "recent_count": rc, "prior_count": pc, "spike": spike, "sample": sample}
    except Exception:
        return {"ok": False}

def keywords_from_market(m: Dict[str, Any]) -> str:
    q = (m.get("question") or "").lower()
    slug = (m.get("slug") or "").replace("-", " ").lower()
    text = f"{q} {slug}"

    if "bitcoin" in text or "btc" in text:
        return "bitcoin OR btc"
    if "ethereum" in text or "eth" in text:
        return "ethereum OR eth"
    if "trump" in text:
        return "trump"
    if "iran" in text:
        return "iran"
    if "ukraine" in text:
        return "ukraine"
    if "election" in text:
        return "election"

    words = [w for w in slug.split() if len(w) >= 4][:4]
    return " ".join(words) if words else ""

# =========================
# DECISION (systematic estimate)
# =========================
def estimate_probability(m: Dict[str, Any], intel: Dict[str, Any]) -> Tuple[float, float, List[str]]:
    reasons: List[str] = []
    p_market = clamp(safe_float(m.get("yes_price")), 0.01, 0.99)

    deltas = m.get("_deltas") or {}
    vol_delta = safe_float(deltas.get("vol_delta"))
    pm = safe_float(deltas.get("price_move_pct"))

    liq = safe_float(m.get("liquidity"))

    # Confidence (slightly easier to pass now)
    conf = 0.42
    conf += clamp((math.log10(liq + 1) - 3.3) / 3.0, 0.0, 0.26)
    conf += clamp((math.log10(max(vol_delta, 0) + 1) - 2.8) / 4.0, 0.0, 0.20)
    conf -= clamp(abs(pm) / 13.0, 0.0, 0.09)

    p = p_market

    # Intel pulse: modest directional nudge
    if intel.get("ok"):
        spike = safe_float(intel.get("spike"))
        rc = safe_int(intel.get("recent_count"), 0)
        pc = safe_int(intel.get("prior_count"), 0)

        if rc >= 8 and spike >= 0.25:
            conf += 0.08
            reasons.append(f"News pulse ↑ (last {GDELT_WINDOW_H}h: {rc} vs prev: {pc}).")
        elif rc >= 8:
            conf += 0.04
            reasons.append(f"News flow steady (last {GDELT_WINDOW_H}h: {rc}).")

        nudge = clamp(spike, -0.5, 1.8) * 0.023  # up to ~4pp
        if pm > 0:
            p += max(0.0, nudge)
            if nudge > 0:
                reasons.append("YES repricing + attention rising (momentum aligns).")
        elif pm < 0:
            p -= max(0.0, nudge)
            if nudge > 0:
                reasons.append("NO repricing + attention rising (momentum aligns).")

        sample = intel.get("sample") or []
        for a in sample[:2]:
            title = (a.get("title") or "").strip()
            url = (a.get("url") or "").strip()
            if title and url:
                reasons.append(f"Source: {title} — {url}")

    # Micro-adjustment from momentum only (kept small)
    p += clamp(pm / 100.0, -0.025, 0.025) * 0.55  # about ±1.4pp max

    if abs(pm) >= MIN_PRICE_MOVE_PCT:
        reasons.append(f"Price move snapshot: {pm:+.2f}% (YES).")
    if vol_delta >= MIN_VOL_DELTA:
        reasons.append(f"Volume delta: +{vol_delta:,.0f} (since last check).")

    p = clamp(p, 0.01, 0.99)
    conf = clamp(conf, 0.34, 0.92)
    if not reasons:
        reasons = ["Low-signal market right now (limited evidence)."]
    return p, conf, reasons

def decide(m: Dict[str, Any], p_model: float, conf: float) -> Optional[Dict[str, Any]]:
    p_market = clamp(safe_float(m.get("yes_price")), 0.01, 0.99)
    edge_pp = (p_model - p_market) * 100.0

    if conf < CONF_MIN:
        return None
    if abs(edge_pp) < EDGE_MIN_PP:
        return None
    if safe_float(m.get("liquidity")) < MIN_LIQUIDITY:
        return None

    side = "YES" if edge_pp > 0 else "NO"
    return {"side": side, "edge_pp": edge_pp, "p_market": p_market, "p_model": p_model, "conf": conf}

def human_side(m: Dict[str, Any], side: str) -> str:
    q = (m.get("question") or "").lower()
    slug = (m.get("slug") or "").lower()
    text = f"{side}"

    if "bitcoin" in q or "btc" in q or "bitcoin" in slug or "btc" in slug:
        if "above" in q or "over" in q or "reach" in q or "at least" in q:
            text = "APOSTAR EM SUBIR/ACIMA (YES)" if side == "YES" else "APOSTAR EM NÃO SUBIR/NÃO CHEGAR (NO)"
        elif "below" in q or "under" in q:
            text = "APOSTAR EM FICAR ABAIXO (YES)" if side == "YES" else "APOSTAR EM NÃO FICAR ABAIXO (NO)"
    return text

# =========================
# PAPER TRADING
# =========================
def paper_pnl(side: str, entry_price: float, exit_price: float, stake_usd: float) -> float:
    entry_price = clamp(entry_price, 0.01, 0.99)
    exit_price = clamp(exit_price, 0.01, 0.99)

    if side == "YES":
        shares = stake_usd / entry_price
        return shares * exit_price - stake_usd
    else:
        entry_no = clamp(1.0 - entry_price, 0.01, 0.99)
        exit_no = clamp(1.0 - exit_price, 0.01, 0.99)
        shares = stake_usd / entry_no
        return shares * exit_no - stake_usd

def maybe_close_old_paper_trades(markets_by_id: Dict[str, Dict[str, Any]]) -> List[str]:
    msgs: List[str] = []
    for t in db_open_paper_trades():
        created_at = safe_int(t["created_at"])
        age_h = (ts() - created_at) / 3600.0
        if age_h < PAPER_EXIT_HOURS:
            continue

        mid = t["market_id"]
        m = markets_by_id.get(mid)
        if not m:
            continue

        entry_price = safe_float(t["entry_price"])
        exit_price = safe_float(m.get("yes_price"))
        side = t["side"]
        stake = safe_float(t["stake_usd"])
        pnl = paper_pnl(side, entry_price, exit_price, stake)

        db_close_paper_trade(int(t["id"]), exit_price, pnl)
        msgs.append(
            f"🧾 PAPER EXIT\n"
            f"- {m.get('question','')[:90]}\n"
            f"- Side: {side}\n"
            f"- Entry: {entry_price:.3f} | Exit: {exit_price:.3f}\n"
            f"- PnL ≈ {pnl:+.2f} USD (stake {stake:.0f})\n"
            f"- {m.get('url')}"
        )
    return msgs

# =========================
# ALERT / WATCHLIST
# =========================
def should_alert(m: Dict[str, Any], decision_obj: Dict[str, Any]) -> bool:
    recent = db_recent_alert(m["id"])
    if not recent:
        return True

    old_edge = safe_float(recent["edge_pp"])
    new_edge = safe_float(decision_obj["edge_pp"])

    if abs(new_edge) >= abs(old_edge) + REALERT_EDGE_BUMP_PP:
        return True
    return False

def format_alert(m: Dict[str, Any], dec: Dict[str, Any], reasons: List[str]) -> str:
    score = safe_float(m.get("_score"))
    deltas = m.get("_deltas") or {}
    vol_delta = safe_float(deltas.get("vol_delta"))
    pm = safe_float(deltas.get("price_move_pct"))

    side = dec["side"]
    edge_pp = dec["edge_pp"]
    p_market = dec["p_market"] * 100.0
    p_model = dec["p_model"] * 100.0
    conf = dec["conf"]

    action_line = "✅ ACTION: consider entry now" if abs(edge_pp) >= (EDGE_MIN_PP + 1.8) else "👀 ACTION: watch 2–5 min"
    side_h = human_side(m, side)

    rs = reasons[:5]
    rs_txt = "\n".join([f"• {r}" for r in rs])

    return (
        f"🚨 EDGE ALERT\n"
        f"{action_line}\n\n"
        f"🎯 Recommendation: {side_h}\n"
        f"🧮 Edge: {edge_pp:+.1f}pp | Market≈{p_market:.1f}% vs Model≈{p_model:.1f}% | Conf={conf:.2f}\n"
        f"📊 Score={score:.2f} | VolΔ={vol_delta:,.0f} | PriceMove={pm:+.2f}% | Liq={safe_float(m.get('liquidity')):,.0f}\n\n"
        f"🧠 Why:\n{rs_txt}\n\n"
        f"🔗 {m.get('url')}"
    )

def format_watchlist(candidates: List[Dict[str, Any]]) -> str:
    topk = candidates[:WATCHLIST_TOP_K]
    lines = []
    for mm in topk:
        d = mm.get("_deltas") or {}
        lines.append(
            f"- {mm.get('question','')[:64]} | score {safe_float(mm.get('_score')):.1f} | "
            f"YES {safe_float(mm.get('yes_price')):.3f} | "
            f"VolΔ {safe_float(d.get('vol_delta')):,.0f} | "
            f"Move {safe_float(d.get('price_move_pct')):+.2f}%"
        )
    digest = "\n".join(lines) if lines else "- (no candidates)"
    return (
        "👀 WATCHLIST (no edge strong enough yet)\n"
        "Top markets to monitor right now:\n"
        f"{digest}\n\n"
        "Tip: if volume spikes again OR price moves ~0.8–1.2% more, it may trigger an EDGE alert."
    )

# =========================
# RUNNERS
# =========================
def run_scanner() -> None:
    db_init()

    markets = fetch_markets(limit=MAX_MARKETS_FETCH, active_only=ACTIVE_ONLY)
    if not markets:
        tg_send("⚠️ Bot: No markets returned from Gamma API. Check GAMMA_BASE/connectivity.")
        return

    # Insert snapshots first (build deltas for next run)
    for m in markets:
        db_insert_snapshot(m)

    markets_by_id = {m["id"]: m for m in markets}

    # Close old paper trades (limited messages)
    exit_msgs = maybe_close_old_paper_trades(markets_by_id)
    for msg in exit_msgs[:6]:
        tg_send(msg)

    candidates = pick_candidates(markets)

    if not candidates:
        tg_send("🤖 Scanner: no candidates passed filters right now.")
        return

    sent = 0
    for m in candidates:
        intel_query = keywords_from_market(m)
        intel = gdelt_pulse(intel_query, hours=GDELT_WINDOW_H) if intel_query else {"ok": False}

        p_model, conf, reasons = estimate_probability(m, intel)
        dec = decide(m, p_model, conf)
        if not dec:
            continue

        if not should_alert(m, dec):
            continue

        tg_send(format_alert(m, dec, reasons))
        db_insert_alert(m["id"], m.get("slug", ""), float(dec["edge_pp"]), float(m.get("_score", 0.0)))
        sent += 1

        if DRY_RUN == "1":
            db_create_paper_trade(
                market_id=m["id"],
                slug=m.get("slug", ""),
                side=dec["side"],
                entry_price=safe_float(m.get("yes_price")),
                stake_usd=PAPER_STAKE_USD,
                meta={
                    "score": m.get("_score"),
                    "edge_pp": dec["edge_pp"],
                    "p_market": dec["p_market"],
                    "p_model": dec["p_model"],
                    "conf": dec["conf"],
                    "reasons": reasons[:8],
                    "url": m.get("url")
                }
            )

        if sent >= MAX_ALERTS_PER_RUN:
            break

    if sent == 0 and SEND_WATCHLIST_IF_NO_ALERTS:
        tg_send(format_watchlist(candidates))

def run_midday() -> None:
    db_init()
    stats = db_paper_stats(days=7)
    open_trades = db_open_paper_trades()
    tg_send(
        "🕛 MIDDAY CHECK\n"
        f"- Open paper trades: {len(open_trades)}\n"
        f"- Closed (7d): {stats['n']}\n"
        f"- Winrate: {stats['winrate']*100:.1f}% | ROI: {stats['roi']*100:.1f}%\n"
        f"- PnL: {stats['pnl']:+.2f} USD | MaxDD: {stats['max_dd']:.2f}"
    )

def run_report() -> None:
    db_init()
    stats = db_paper_stats(days=7)

    cutoff = ts() - 86400
    conn = db_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM alerts_sent
        WHERE created_at>=?
        ORDER BY created_at DESC
        LIMIT 12
    """, (cutoff,))
    rows = cur.fetchall()
    conn.close()

    lines = []
    for r in rows:
        lines.append(f"- {r['slug'] or r['market_id']} | edge {safe_float(r['edge_pp']):+.1f}pp | score {safe_float(r['score']):.1f}")
    digest = "\n".join(lines) if lines else "- (no alerts in last 24h)"

    tg_send(
        "📌 DAILY REPORT\n"
        f"- Paper closed (7d): {stats['n']}\n"
        f"- Winrate: {stats['winrate']*100:.1f}% | ROI: {stats['roi']*100:.1f}%\n"
        f"- PnL: {stats['pnl']:+.2f} USD | Avg/trade: {stats['avg_pnl']:+.2f} | MaxDD: {stats['max_dd']:.2f}\n\n"
        "🔔 Last 24h alerts:\n"
        f"{digest}"
    )

def run_night() -> None:
    run_scanner()

def main() -> None:
    try:
        if MODE == "SCANNER":
            run_scanner()
        elif MODE == "MIDDAY":
            run_midday()
        elif MODE == "REPORT":
            run_report()
        elif MODE == "NIGHT":
            run_night()
        else:
            tg_send(f"⚠️ Unknown MODE={MODE}. Use SCANNER|MIDDAY|REPORT|NIGHT.")
    except Exception as e:
        err = "".join(traceback.format_exception(type(e), e, e.__traceback__))[-3500:]
        tg_send("❌ Bot crashed:\n" + err)
        raise

if __name__ == "__main__":
    if len(sys.argv) >= 2:
        MODE = sys.argv[1].strip().upper()
    main()
