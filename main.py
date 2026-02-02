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
# SETTINGS (equilíbrio: alerta útil)
# =========================
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "30"))

# filtros mínimos (mantém amplo)
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "0"))
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "300"))  # ↑ sobe um pouco pra reduzir ruído

# gatilhos
PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "0.004"))  # 0.4%
VOLUME_JUMP = float(os.environ.get("VOLUME_JUMP", "250"))          # +250

# NOVO: filtros de preço e movimento absoluto (mata odds absurdas + micro moves)
MIN_YES_PRICE = float(os.environ.get("MIN_YES_PRICE", "0.05"))     # ignora YES < 0.05
MAX_YES_PRICE = float(os.environ.get("MAX_YES_PRICE", "0.95"))     # ignora YES > 0.95
MIN_ABS_MOVE = float(os.environ.get("MIN_ABS_MOVE", "0.01"))       # exige pelo menos +0.01 (1 cent)
# exemplo: 0.01->0.012 (abs=0.002) NÃO alerta mesmo sendo +20%

# Histórico última 1 hora
HIST_POINTS = int(os.environ.get("HIST_POINTS", str(max(30, int(3600 / max(5, SCAN_SECONDS))))))  # ~1h
MIN_RANGE_PCT = float(os.environ.get("MIN_RANGE_PCT", "0.03"))  # exige range >= 3% na hora pra não alertar chato

# Anti-spam
COOLDOWN_PRICE_MIN = int(os.environ.get("COOLDOWN_PRICE_MIN", "2"))
COOLDOWN_VOLUME_MIN = int(os.environ.get("COOLDOWN_VOLUME_MIN", "2"))

MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "8"))

# Status / health
HEALTH_EVERY_MIN = int(os.environ.get("HEALTH_EVERY_MIN", "10"))
STATUS_EVERY_SCANS = int(os.environ.get("STATUS_EVERY_SCANS", "10"))

DEBUG = os.environ.get("DEBUG", "1") == "1"

# Excluir sports
EXCLUDE_SPORTS = os.environ.get("EXCLUDE_SPORTS", "1") == "1"
SPORTS_TAG_SLUG = os.environ.get("SPORTS_TAG_SLUG", "sports").strip().lower()

# Opcional: filtro inteligente por "volatilidade" do próprio market
USE_VOL_FILTER = os.environ.get("USE_VOL_FILTER", "1") == "1"
VOL_SIGMA_MULT = float(os.environ.get("VOL_SIGMA_MULT", "2.5"))  # quanto maior, menos alertas
MIN_POINTS_FOR_VOL = int(os.environ.get("MIN_POINTS_FOR_VOL", "30"))

# =========================
# STATE
# =========================
last_state = {}   # market_id -> {"price": float, "volume": float, "ts": float}
cooldowns = {}    # (market_id, typ) -> last_sent_ts

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

def can_send(market_id: str, typ: str, cooldown_min: int) -> bool:
    key = (market_id, typ)
    last = cooldowns.get(key, 0)
    return (now_ts() - last) >= (cooldown_min * 60)

def mark_sent(market_id: str, typ: str):
    cooldowns[(market_id, typ)] = now_ts()

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
    long = prices[-50:] if len(prices) >= 50 else prices
    trend = mean(short) - mean(long)
    return {"high": hi, "low": lo, "range_pct": range_pct, "pos": pos, "trend": trend}

def movement_is_significant(price_hist_deque, abs_move, pct_move):
    """
    Filtro inteligente: só alerta se o movimento for maior que o "normal" do market.
    Usa desvio padrão das diferenças de preço (últimos N pontos).
    """
    if not USE_VOL_FILTER:
        return True
    if price_hist_deque is None or len(price_hist_deque) < MIN_POINTS_FOR_VOL:
        return True

    prices = list(price_hist_deque)
    diffs = [abs(prices[i] - prices[i-1]) for i in range(1, len(prices))]
    sigma = stdev(diffs)
    # se sigma for 0, não filtra
    if sigma <= 0:
        return True
    # exige movimento acima de múltiplos do sigma OU acima do pct baseline
    return (abs_move >= (VOL_SIGMA_MULT * sigma)) or (pct_move >= PRICE_MOVE_PCT * 1.5)

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
    try:
        if "tag_id" in m and int(m.get("tag_id")) == int(tag_id):
            return True
    except Exception:
        pass
    tag_ids = m.get("tagIds") or m.get("tag_ids")
    if isinstance(tag_ids, list):
        for x in tag_ids:
            try:
                if int(x) == int(tag_id):
                    return True
            except Exception:
                continue
    return False

# =========================
# RECOMENDAÇÃO CLARA (com histórico)
# =========================
def classify_action(pct_move, abs_move, delta_vol, liquidity, hm):
    # proteção: liquidez baixa + movimento = ruído
    if liquidity < 300 and (pct_move >= 0.006 or abs_move >= 0.02):
        return "IGNORAR (liq baixa: provável ruído)"

    # sem histórico suficiente
    if hm is None:
        if pct_move >= 0.01 and abs_move >= 0.02 and delta_vol >= 500:
            return "FORTE (checar agora + acompanhar 2 min)"
        if pct_move >= 0.008 and abs_move >= 0.015:
            return "CONFIRMAR (acompanhar 120s)"
        return "OBSERVAR"

    # com histórico 1h
    range_ok = hm["range_pct"] >= MIN_RANGE_PCT
    near_top = range_ok and hm["pos"] >= 0.85
    near_bottom = range_ok and hm["pos"] <= 0.15
    trending_up = hm["trend"] > 0
    trending_down = hm["trend"] < 0

    if pct_move >= 0.01 and abs_move >= 0.02 and delta_vol >= 500 and liquidity >= 800:
        if near_top and trending_up:
            return "CONFIRMAR (perto da máxima 1h)"
        if near_bottom and trending_up:
            return "FORTE (saindo do fundo 1h)"
        return "FORTE (checar agora + acompanhar 2 min)"

    if pct_move >= 0.008 and abs_move >= 0.015:
        if near_top:
            return "OBSERVAR (topo do range 1h)"
        if near_bottom:
            return "OBSERVAR (fundo do range 1h)"
        return "CONFIRMAR (acompanhar 120s)"

    return "OBSERVAR"

def hist_line(hm):
    if hm is None:
        return ""
    if hm["range_pct"] < MIN_RANGE_PCT:
        return ""
    pos_pct = hm["pos"] * 100
    return f"\n📈 1h Range: low={hm['low']:.3f} high={hm['high']:.3f} | pos={pos_pct:.0f}%"

# =========================
# ALERT FORMAT
# =========================
def alert_price(m, oldp, newp, pct, abs_move, vol, liq, delta_vol, hm):
    title = m.get("question") or m.get("title") or "Mercado"
    direction = "⬆️" if newp > oldp else "⬇️"
    action = classify_action(pct, abs_move, delta_vol, liq, hm)
    return (
        f"🚨 ALERTA | PREÇO ({pct*100:.2f}%, Δ={abs_move:.3f})\n"
        f"🎯 RECOMENDAÇÃO: {action}\n"
        f"{title}\n"
        f"{direction} {oldp:.3f} → {newp:.3f}\n"
        f"ΔVol: +{int(delta_vol)} | Liq: {int(liq)}"
        f"{hist_line(hm)}\n"
        f"{market_link(m)}"
    )

def alert_volume(m, oldv, newv, delta_vol, price, liq, pct_move, abs_move, hm):
    title = m.get("question") or m.get("title") or "Mercado"
    action = classify_action(pct_move, abs_move, delta_vol, liq, hm)
    return (
        f"🚨 ALERTA | VOLUME (+{int(delta_vol)})\n"
        f"🎯 RECOMENDAÇÃO: {action}\n"
        f"{title}\n"
        f"Vol: {int(oldv)} → {int(newv)} | YES: {price:.3f}\n"
        f"PreçoΔ%: {pct_move*100:.2f}% | ΔAbs: {abs_move:.3f} | Liq: {int(liq)}"
        f"{hist_line(hm)}\n"
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
            f"Time: {fmt_time(now)} | Interval: {SCAN_SECONDS}s | hist_points={HIST_POINTS}"
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
        markets += fetch_markets_page(limit=200, offset=400)
    except Exception as e:
        send(f"⚠️ Erro ao buscar mercados: {e}")
        return

    # contadores
    c_total = 0
    c_active = 0
    c_sports_out = 0
    c_price_ok = 0
    c_ok = 0
    c_has_price = 0
    c_ready = 0
    trig_p = 0
    trig_v = 0

    candidates = []  # (score, type, market, payload)

    for m in markets:
        c_total += 1

        # ativo (filtro local)
        if m.get("active") is True:
            c_active += 1
        else:
            continue

        # exclui sports
        if EXCLUDE_SPORTS and sports_tag_id is not None and market_has_tag_id(m, sports_tag_id):
            c_sports_out += 1
            continue

        vol = get_num(m, "volume", 0)
        liq = get_num(m, "liquidity", 0)
        if vol < MIN_VOLUME or liq < MIN_LIQUIDITY:
            continue
        c_ok += 1

        price = get_yes_price(m)
        if price is None:
            continue
        c_has_price += 1

        # NOVO: filtra odds extremas (muito baixo ou muito alto)
        if price < MIN_YES_PRICE or price > MAX_YES_PRICE:
            continue
        c_price_ok += 1

        market_id = str(m.get("id", "")).strip()
        if not market_id:
            continue

        prev = last_state.get(market_id)
        t = now_ts()

        # histórico 1h
        ph = push_hist(price_hist, market_id, price, HIST_POINTS)
        _ = push_hist(vol_hist, market_id, vol, HIST_POINTS)
        hm = hist_metrics(ph)

        # NOVO: se o range 1h for muito pequeno, ignora (corta mercados “parados”)
        if hm is not None and hm["range_pct"] < MIN_RANGE_PCT:
            continue

        if prev is None:
            last_state[market_id] = {"price": price, "volume": vol, "ts": t}
            continue

        c_ready += 1

        oldp = prev["price"]
        oldv = prev["volume"]

        # update state
        last_state[market_id] = {"price": price, "volume": vol, "ts": t}

        abs_move = abs(price - oldp)
        pct_move = abs_move / oldp if oldp > 0 else 0.0
        dv = vol - oldv

        # NOVO: exige movimento absoluto mínimo (mata “% fake” em odds baixas)
        # (aplica tanto para gatilho preço quanto para volume como "confirmação")
        abs_ok = abs_move >= MIN_ABS_MOVE

        # gatilho preço (precisa pct OU abs, mas vamos exigir abs_ok)
        if abs_ok and (pct_move >= PRICE_MOVE_PCT) and can_send(market_id, "price", COOLDOWN_PRICE_MIN):
            # NOVO: filtro inteligente por volatilidade (reduz micro ruído)
            if movement_is_significant(ph, abs_move, pct_move):
                trig_p += 1
                score = (pct_move * 100) + (abs_move * 50) + (liq / 1500) + (vol / 15000)
                candidates.append((score, "price", m, (oldp, price, pct_move, abs_move, vol, liq, dv, hm)))

        # gatilho volume (exige dv + também exige abs_ok OU pct acima de um mínimo)
        if (dv >= VOLUME_JUMP) and can_send(market_id, "volume", COOLDOWN_VOLUME_MIN):
            if abs_ok or pct_move >= (PRICE_MOVE_PCT * 0.8):
                if movement_is_significant(ph, abs_move, pct_move):
                    trig_v += 1
                    score = (dv / 150) + (pct_move * 60) + (abs_move * 30) + (liq / 1500)
                    candidates.append((score, "volume", m, (oldv, vol, dv, price, liq, pct_move, abs_move, hm)))

    log(
        f"[scan {scan_count}] total={c_total} active={c_active} sports_out={c_sports_out} "
        f"ok={c_ok} has_price={c_has_price} price_ok={c_price_ok} ready={c_ready} "
        f"trigP={trig_p} trigV={trig_v} cand={len(candidates)}"
    )

    # status no Telegram
    if scan_count % STATUS_EVERY_SCANS == 0:
        send(
            f"🧾 Status | {fmt_time(now_ts())}\n"
            f"active={c_active} ok={c_ok} price_ok={c_price_ok} ready={c_ready}\n"
            f"sports_out={c_sports_out} cand={len(candidates)} | trigP={trig_p} trigV={trig_v}"
        )

    if not candidates:
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:MAX_ALERTS_PER_SCAN]

    send(f"🔔 Scan: {len(top)} alerta(s) | {fmt_time(now_ts())}")

    sent_now = 0
    for _, typ, m, payload in top:
        market_id = str(m.get("id", "")).strip()
        if not market_id:
            continue

        if typ == "price":
            oldp, newp, pct, abs_move, vol, liq, dv, hm = payload
            send(alert_price(m, oldp, newp, pct, abs_move, vol, liq, dv, hm))
            mark_sent(market_id, "price")
            sent_now += 1
        else:
            oldv, newv, dv, price, liq, pct_move, abs_move, hm = payload
            send(alert_volume(m, oldv, newv, dv, price, liq, pct_move, abs_move, hm))
            mark_sent(market_id, "volume")
            sent_now += 1

    alert_count += sent_now

# =========================
# MAIN
# =========================
def main():
    global sports_tag_id

    send("🔎 Iniciando...")

    if EXCLUDE_SPORTS:
        sports_tag_id = fetch_tag_id_by_slug(SPORTS_TAG_SLUG)
        if sports_tag_id is None:
            send("⚠️ Não achei tag 'sports'. Vou excluir só pelo 'active/closed'.")
        else:
            send(f"✅ Excluindo sports: tag_id={sports_tag_id}")

    send(
        "🤖 Bot ligado (menos ruído)\n"
        f"Scan={SCAN_SECONDS}s | hist=1h (~{HIST_POINTS} pts)\n"
        f"Filtros: YES {MIN_YES_PRICE:.2f}–{MAX_YES_PRICE:.2f} | abs_move≥{MIN_ABS_MOVE:.3f} | 1h_range≥{MIN_RANGE_PCT*100:.1f}%\n"
        f"Gatilhos: pct≥{PRICE_MOVE_PCT*100:.2f}% | volΔ≥{int(VOLUME_JUMP)} | liq≥{int(MIN_LIQUIDITY)}\n"
        f"VolFilter: {'ON' if USE_VOL_FILTER else 'OFF'} (x{VOL_SIGMA_MULT})"
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
