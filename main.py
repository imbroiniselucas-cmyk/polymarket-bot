import os
import time
import json
import math
import requests
import telebot
from datetime import datetime
from collections import deque

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
if not TOKEN or not CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

bot = telebot.TeleBot(TOKEN)

GAMMA_BASE = "https://gamma-api.polymarket.com"
MARKETS_URL = f"{GAMMA_BASE}/markets"
TAGS_URL = f"{GAMMA_BASE}/tags"

# =========================
# SETTINGS (mais filtrado)
# =========================
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "30"))

# filtros de mercado (reduz muito o ruído)
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "10000"))
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "80000"))

# odds extremas fora
MIN_YES_PRICE = float(os.environ.get("MIN_YES_PRICE", "0.10"))
MAX_YES_PRICE = float(os.environ.get("MAX_YES_PRICE", "0.90"))

# gatilhos "adultos"
PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "0.015"))  # 1.5%
MIN_ABS_MOVE = float(os.environ.get("MIN_ABS_MOVE", "0.030"))      # 0.03
VOLUME_JUMP = float(os.environ.get("VOLUME_JUMP", "5000"))         # +5000

# histórico 1h
HIST_POINTS = int(os.environ.get("HIST_POINTS", str(max(60, int(3600 / max(5, SCAN_SECONDS))))))
MIN_RANGE_PCT = float(os.environ.get("MIN_RANGE_PCT", "0.05"))  # 5% range na última hora

# filtro por volatilidade do próprio market (corta micro-ruído)
USE_VOL_FILTER = os.environ.get("USE_VOL_FILTER", "1") == "1"
VOL_SIGMA_MULT = float(os.environ.get("VOL_SIGMA_MULT", "3.0"))
MIN_POINTS_FOR_VOL = int(os.environ.get("MIN_POINTS_FOR_VOL", "60"))

# score mínimo (régua final)
SCORE_MIN = float(os.environ.get("SCORE_MIN", "12.0"))

# anti-spam por mercado
COOLDOWN_MIN = int(os.environ.get("COOLDOWN_MIN", "20"))  # 20 min

# limites
MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "3"))

# status/health
STATUS_EVERY_SCANS = int(os.environ.get("STATUS_EVERY_SCANS", "10"))
HEALTH_EVERY_MIN = int(os.environ.get("HEALTH_EVERY_MIN", "15"))

DEBUG = os.environ.get("DEBUG", "1") == "1"

# excluir sports
EXCLUDE_SPORTS = os.environ.get("EXCLUDE_SPORTS", "1") == "1"
SPORTS_TAG_SLUG = os.environ.get("SPORTS_TAG_SLUG", "sports").strip().lower()

# =========================
# STATE
# =========================
last_state = {}   # market_id -> {"price": float, "volume": float, "ts": float}
cooldowns = {}    # market_id -> last_sent_ts

price_hist = {}   # market_id -> deque(prices)
vol_hist = {}     # market_id -> deque(volumes)

start_ts = time.time()
scan_count = 0
alert_count = 0
last_health_ts = 0
sports_tag_id = None

# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return time.time()

def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")

def send(msg: str):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

def log(msg: str):
    if DEBUG:
        print(msg, flush=True)

def get_num(obj, key, default=0.0) -> float:
    try:
        return float(obj.get(key, default) or default)
    except Exception:
        return float(default)

def parse_outcome_prices(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except Exception:
            return None
    if isinstance(raw, str):
        s = raw.strip()
        try:
            val = json.loads(s)
            if isinstance(val, list):
                return [float(x) for x in val]
        except Exception:
            pass
        try:
            parts = [p.strip() for p in s.strip("[]").split(",")]
            nums = [float(p) for p in parts if p]
            return nums if nums else None
        except Exception:
            return None
    return None

def get_yes_price(m):
    prices = parse_outcome_prices(m.get("outcomePrices"))
    if not prices or len(prices) < 1:
        return None
    try:
        return float(prices[0])
    except Exception:
        return None

def market_link(m):
    slug = (m.get("slug") or "").strip()
    return f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"

def push_hist(dct, key, value, maxlen):
    dq = dct.get(key)
    if dq is None:
        dq = deque(maxlen=maxlen)
        dct[key] = dq
    dq.append(value)
    return dq

def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0

def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return math.sqrt(var)

def hist_metrics(prices_deque):
    if prices_deque is None or len(prices_deque) < 10:
        return None
    prices = list(prices_deque)
    hi = max(prices)
    lo = min(prices)
    if lo <= 0:
        return None
    range_pct = (hi - lo) / lo
    cur = prices[-1]
    pos = 0.5 if hi == lo else (cur - lo) / (hi - lo)
    short = prices[-10:]
    long = prices[-60:] if len(prices) >= 60 else prices
    trend = mean(short) - mean(long)
    return {"high": hi, "low": lo, "range_pct": range_pct, "pos": pos, "trend": trend}

def movement_is_significant(price_hist_deque, abs_move):
    if not USE_VOL_FILTER:
        return True
    if price_hist_deque is None or len(price_hist_deque) < MIN_POINTS_FOR_VOL:
        return True
    prices = list(price_hist_deque)
    diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    sigma = stdev(diffs)
    if sigma <= 0:
        return True
    return abs_move >= (VOL_SIGMA_MULT * sigma)

def can_send_market(market_id: str) -> bool:
    last = cooldowns.get(market_id, 0)
    return (now_ts() - last) >= (COOLDOWN_MIN * 60)

def mark_sent_market(market_id: str):
    cooldowns[market_id] = now_ts()

def fetch_tag_id_by_slug(slug: str):
    try:
        r = requests.get(f"{TAGS_URL}/slug/{slug}", timeout=30)
        r.raise_for_status()
        t = r.json()
        return int(t["id"])
    except Exception:
        pass
    r = requests.get(TAGS_URL, params={"limit": 5000}, timeout=30)
    r.raise_for_status()
    tags = r.json()
    for t in tags:
        if (t.get("slug") or "").strip().lower() == slug:
            return int(t["id"])
    return None

def market_has_tag_id(m, tag_id: int) -> bool:
    if tag_id is None:
        return False
    tags = m.get("tags")
    if isinstance(tags, list):
        for t in tags:
            try:
                if int(t.get("id")) == int(tag_id):
                    return True
            except Exception:
                continue
    return False

# =========================
# RECOMENDAÇÃO SUPER CLARA
# =========================
def clear_action_line(hm, direction_up):
    """
    Retorna:
      action (ESPERAR / DESCARTAR),
      motive,
      confirm_level,
      invalid_level
    """
    if hm is None or hm["range_pct"] < MIN_RANGE_PCT:
        return ("ESPERAR", "Sem histórico 1h suficiente", None, None)

    lo, hi = hm["low"], hm["high"]
    r = max(hi - lo, 1e-9)
    pos = hm["pos"]

    confirm = lo + 0.25 * r     # confirmação “reação”
    invalid = lo - 0.005        # invalidação abaixo do low (buffer)
    mid = lo + 0.50 * r
    breakout = hi + 0.005

    # FUNDO da 1h
    if pos <= 0.15:
        if direction_up:
            return ("ESPERAR", "Saindo do FUNDO 1h", confirm, lo)
        return ("DESCARTAR", "Queda no FUNDO 1h (não perseguir)", confirm, invalid)

    # TOPO da 1h
    if pos >= 0.85:
        return ("ESPERAR", "Perto do TOPO 1h", breakout, mid)

    # MEIO do range
    return ("ESPERAR", "Meio do range 1h", mid, mid)

def format_alert(m, oldp, newp, oldv, newv, hm, score):
    title = m.get("question") or m.get("title") or "Mercado"
    direction_up = newp > oldp
    arrow = "⬆️" if direction_up else "⬇️"
    momentum = "YES↑" if direction_up else "YES↓"

    abs_move = abs(newp - oldp)
    pct_move = (abs_move / oldp) if oldp > 0 else 0.0
    dv = newv - oldv
    liq = int(get_num(m, "liquidity", 0))
    vol_total = int(get_num(m, "volume", 0))

    action, motive, confirm_level, invalid_level = clear_action_line(hm, direction_up)

    rules = ""
    if confirm_level is not None and invalid_level is not None:
        rules = f"REGRAS: só agir se YES>{confirm_level:.3f} por 2 min | se YES<{invalid_level:.3f} → DESCARTAR"
    elif confirm_level is not None:
        rules = f"REGRAS: só agir se YES>{confirm_level:.3f} por 2 min"

    hist_txt = ""
    if hm and hm["range_pct"] >= MIN_RANGE_PCT:
        hist_txt = f"1H: low={hm['low']:.3f} high={hm['high']:.3f} pos={hm['pos']*100:.0f}%"

    return (
        f"🚨 ALERTA | {momentum} | score={score:.1f}\n"
        f"AÇÃO AGORA: {action}\n"
        f"MOTIVO: {motive}\n"
        f"{rules}\n"
        f"{hist_txt}\n\n"
        f"{title}\n"
        f"{arrow} {oldp:.3f} → {newp:.3f}  (Δ={abs_move:.3f}, {pct_move*100:.2f}%)\n"
        f"ΔVol:+{int(dv)} | Liq:{liq} | VolTotal:{vol_total}\n"
        f"{market_link(m)}"
    )

# =========================
# HEALTH / STATUS
# =========================
def healthcheck():
    global last_health_ts
    now = now_ts()
    if last_health_ts == 0:
        last_health_ts = now
        return
    if (now - last_health_ts) >= (HEALTH_EVERY_MIN * 60):
        uptime_min = int((now - start_ts) / 60)
        send(
            f"📡 Health\n"
            f"Uptime: {uptime_min}m | Scans: {scan_count} | Alerts: {alert_count}\n"
            f"Interval: {SCAN_SECONDS}s | hist_points={HIST_POINTS}"
        )
        last_health_ts = now

# =========================
# FETCH MARKETS (paginado)
# =========================
def fetch_markets_page(limit: int = 200, offset: int = 0):
    params = {
        "closed": "false",
        "limit": int(limit),
        "offset": int(offset),
        "order": "volume24hr",
        "ascending": "false",
    }
    r = requests.get(MARKETS_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "markets" in data:
        return data["markets"]
    return data

# =========================
# SCAN
# =========================
def scan_once():
    global scan_count, alert_count
    scan_count += 1

    markets = []
    try:
        markets += fetch_markets_page(limit=200, offset=0)
        markets += fetch_markets_page(limit=200, offset=200)
    except Exception as e:
        send(f"⚠️ Erro ao buscar mercados: {e}")
        return

    c_total = 0
    c_active = 0
    c_sports_out = 0
    c_price = 0
    c_ready = 0
    candidates = []

    for m in markets:
        c_total += 1
        if m.get("active") is not True:
            continue
        c_active += 1

        if EXCLUDE_SPORTS and sports_tag_id is not None and market_has_tag_id(m, sports_tag_id):
            c_sports_out += 1
            continue

        liq = get_num(m, "liquidity", 0)
        vol_total = get_num(m, "volume", 0)
        if liq < MIN_LIQUIDITY or vol_total < MIN_VOLUME:
            continue

        price = get_yes_price(m)
        if price is None:
            continue
        c_price += 1

        if price < MIN_YES_PRICE or price > MAX_YES_PRICE:
            continue

        market_id = str(m.get("id", "")).strip()
        if not market_id:
            continue

        if not can_send_market(market_id):
            continue

        ph = push_hist(price_hist, market_id, price, HIST_POINTS)
        _ = push_hist(vol_hist, market_id, vol_total, HIST_POINTS)
        hm = hist_metrics(ph)

        if hm is not None and hm["range_pct"] < MIN_RANGE_PCT:
            continue

        prev = last_state.get(market_id)
        t = now_ts()
        if prev is None:
            last_state[market_id] = {"price": price, "volume": vol_total, "ts": t}
            continue

        c_ready += 1
        oldp = prev["price"]
        oldv = prev["volume"]
        last_state[market_id] = {"price": price, "volume": vol_total, "ts": t}

        abs_move = abs(price - oldp)
        pct_move = (abs_move / oldp) if oldp > 0 else 0.0
        dv = vol_total - oldv

        # filtros “duros”
        if abs_move < MIN_ABS_MOVE:
            continue
        if pct_move < PRICE_MOVE_PCT:
            continue
        if dv < VOLUME_JUMP:
            continue
        if not movement_is_significant(ph, abs_move):
            continue

        # score final
        score = (abs_move * 80) + (pct_move * 120) + (dv / 1500) + (liq / 20000)
        if score < SCORE_MIN:
            continue

        candidates.append((score, m, oldp, price, oldv, vol_total, hm))

    log(
        f"[scan {scan_count}] total={c_total} active={c_active} sports_out={c_sports_out} "
        f"price={c_price} ready={c_ready} cand={len(candidates)}"
    )

    if scan_count % STATUS_EVERY_SCANS == 0:
        send(
            f"🧾 Status | {fmt_time(now_ts())}\n"
            f"active={c_active} sports_out={c_sports_out} ready={c_ready} cand={len(candidates)}\n"
            f"Filtros: liq≥{int(MIN_LIQUIDITY)} vol≥{int(MIN_VOLUME)} abs≥{MIN_ABS_MOVE:.3f} "
            f"pct≥{PRICE_MOVE_PCT*100:.2f}% dv≥{int(VOLUME_JUMP)} score≥{SCORE_MIN} cooldown={COOLDOWN_MIN}m"
        )

    if not candidates:
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:MAX_ALERTS_PER_SCAN]

    send(f"🔔 Scan: {len(top)} alerta(s) | {fmt_time(now_ts())}")

    for score, m, oldp, newp, oldv, newv, hm in top:
        market_id = str(m.get("id", "")).strip()
        send(format_alert(m, oldp, newp, oldv, newv, hm, score))
        mark_sent_market(market_id)
        alert_count += 1

# =========================
# MAIN
# =========================
def main():
    global sports_tag_id
    send("🔎 Iniciando...")

    if EXCLUDE_SPORTS:
        sports_tag_id = fetch_tag_id_by_slug(SPORTS_TAG_SLUG)
        if sports_tag_id is None:
            send("⚠️ Não achei tag 'sports'. Vou seguir sem excluir por tag.")
        else:
            send(f"✅ Excluindo sports: tag_id={sports_tag_id}")

    send(
        "🤖 Bot ligado (AÇÃO + MOTIVO super claros)\n"
        f"Scan={SCAN_SECONDS}s | hist=1h (~{HIST_POINTS} pts)\n"
        f"Filtros: liq≥{int(MIN_LIQUIDITY)} vol≥{int(MIN_VOLUME)} YES {MIN_YES_PRICE:.2f}-{MAX_YES_PRICE:.2f}\n"
        f"Gatilhos: abs≥{MIN_ABS_MOVE:.3f} pct≥{PRICE_MOVE_PCT*100:.2f}% volΔ≥{int(VOLUME_JUMP)} score≥{SCORE_MIN}\n"
        f"VolFilter: {'ON' if USE_VOL_FILTER else 'OFF'} (x{VOL_SIGMA_MULT}) | cooldown={COOLDOWN_MIN}m | max/scan={MAX_ALERTS_PER_SCAN}"
    )

    while True:
        try:
            scan_once()
            healthcheck()
        except Exception as e:
            send(f"⚠️ Erro: {e}")
            log(f"ERROR: {e}")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
