import os
import time
import json
import requests
import telebot
from datetime import datetime

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
# SETTINGS (AGRESSIVO)
# =========================
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "30"))

# filtros mínimos (agressivo)
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "0"))
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "0"))

# gatilhos (agressivo)
PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "0.003"))  # 0.3%
VOLUME_JUMP = float(os.environ.get("VOLUME_JUMP", "100"))          # +100

# anti-spam (curto)
COOLDOWN_PRICE_MIN = int(os.environ.get("COOLDOWN_PRICE_MIN", "1"))
COOLDOWN_VOLUME_MIN = int(os.environ.get("COOLDOWN_VOLUME_MIN", "1"))

MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "10"))

# status / health (pra você não ficar no escuro)
HEALTH_EVERY_MIN = int(os.environ.get("HEALTH_EVERY_MIN", "10"))
STATUS_EVERY_SCANS = int(os.environ.get("STATUS_EVERY_SCANS", "10"))  # a cada 10 scans (~5 min com 30s)

DEBUG = os.environ.get("DEBUG", "1") == "1"

# tag alvo
CRYPTO_TAG_SLUG = os.environ.get("CRYPTO_TAG_SLUG", "crypto").strip().lower()

# =========================
# STATE
# =========================
last_state = {}    # market_id -> {"price": float, "volume": float, "ts": float}
cooldowns = {}     # (market_id, typ) -> last_sent_ts

start_ts = time.time()
scan_count = 0
alert_count = 0
last_health_ts = 0

crypto_tag_id = None


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
    """
    outcomePrices pode vir como:
      - lista: [0.52, 0.48]
      - string JSON: "[0.52,0.48]"
      - string CSV/estranha: "0.52,0.48"
    """
    if raw is None:
        return None

    if isinstance(raw, list):
        try:
            return [float(x) for x in raw]
        except Exception:
            return None

    if isinstance(raw, str):
        s = raw.strip()
        # tenta JSON primeiro
        try:
            val = json.loads(s)
            if isinstance(val, list):
                return [float(x) for x in val]
        except Exception:
            pass

        # fallback CSV
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
        return float(prices[0])  # YES
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


# =========================
# TAG DISCOVERY
# =========================
def fetch_tag_id_by_slug(slug: str) -> int:
    # endpoint oficial existe: /tags/slug/{slug}, mas pra ser robusto vamos tentar direto e, se falhar, cair no /tags list.
    # (Assim você não depende de um endpoint único)
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

    raise RuntimeError(f"Não achei tag slug='{slug}'")


# =========================
# FETCH MARKETS (CRYPTO via tag_id)
# =========================
def fetch_crypto_markets(limit: int = 200, offset: int = 0):
    # tag_id e closed são suportados. order/ascending/limit/offset também. :contentReference[oaicite:1]{index=1}
    params = {
        "tag_id": int(crypto_tag_id),
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
# ALERT FORMAT
# =========================
def alert_price(m, oldp, newp, pct, vol, liq):
    title = m.get("question") or m.get("title") or "Mercado"
    direction = "⬆️" if newp > oldp else "⬇️"
    return (
        f"🚨 CRYPTO | Preço ({pct*100:.2f}%)\n"
        f"{title}\n"
        f"{direction} {oldp:.3f} → {newp:.3f}\n"
        f"Vol: {int(vol)} | Liq: {int(liq)}\n"
        f"{market_link(m)}"
    )

def alert_volume(m, oldv, newv, dv, price, liq):
    title = m.get("question") or m.get("title") or "Mercado"
    return (
        f"🚨 CRYPTO | Volume (+{int(dv)})\n"
        f"{title}\n"
        f"Vol: {int(oldv)} → {int(newv)} | YES: {price:.3f}\n"
        f"Liq: {int(liq)}\n"
        f"{market_link(m)}"
    )


# =========================
# HEALTHCHECK / STATUS
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
            f"📡 Health (CRYPTO)\n"
            f"Uptime: {uptime_min}m | Scans: {scan_count} | Alerts: {alert_count}\n"
            f"Time: {fmt_time(now)} | Interval: {SCAN_SECONDS}s | tag_id={crypto_tag_id}"
        )
        last_health_ts = now


# =========================
# SCAN
# =========================
def scan_once():
    global scan_count, alert_count
    scan_count += 1

    # Puxa 2 páginas pra aumentar chance de pegar os mais “quentes”
    markets = []
    try:
        markets += fetch_crypto_markets(limit=200, offset=0)
        markets += fetch_crypto_markets(limit=200, offset=200)
    except Exception as e:
        send(f"⚠️ Erro ao buscar mercados: {e}")
        return

    # contadores de diagnóstico
    c_total = 0
    c_active = 0
    c_closed_ok = 0
    c_ok = 0
    c_has_price = 0
    c_ready = 0
    trig_p = 0
    trig_v = 0

    candidates = []  # (score, type, market, payload)

    for m in markets:
        c_total += 1

        # FILTRO LOCAL "ATIVO" (mais seguro do que confiar em query param)
        if m.get("active") is True:
            c_active += 1
        else:
            continue

        if m.get("closed"):
            continue
        c_closed_ok += 1

        vol = get_num(m, "volume", 0)
        liq = get_num(m, "liquidity", 0)
        if vol < MIN_VOLUME or liq < MIN_LIQUIDITY:
            continue
        c_ok += 1

        price = get_yes_price(m)
        if price is None:
            continue
        c_has_price += 1

        market_id = str(m.get("id", "")).strip()
        if not market_id:
            continue

        prev = last_state.get(market_id)
        t = now_ts()

        if prev is None:
            last_state[market_id] = {"price": price, "volume": vol, "ts": t}
            continue

        c_ready += 1

        oldp = prev["price"]
        oldv = prev["volume"]

        # update state
        last_state[market_id] = {"price": price, "volume": vol, "ts": t}

        pct = abs(price - oldp) / oldp if oldp > 0 else 0.0
        dv = vol - oldv

        if pct >= PRICE_MOVE_PCT and can_send(market_id, "price", COOLDOWN_PRICE_MIN):
            trig_p += 1
            score = (pct * 100) + (liq / 1500) + (vol / 15000)
            candidates.append((score, "price", m, (oldp, price, pct, vol, liq)))

        if dv >= VOLUME_JUMP and can_send(market_id, "volume", COOLDOWN_VOLUME_MIN):
            trig_v += 1
            score = (dv / 100) + (liq / 1500) + (pct * 50)
            candidates.append((score, "volume", m, (oldv, vol, dv, price, liq)))

    # log no console
    log(
        f"[scan {scan_count}] total={c_total} active={c_active} closed_ok={c_closed_ok} ok={c_ok} "
        f"has_price={c_has_price} ready={c_ready} trigP={trig_p} trigV={trig_v} cand={len(candidates)}"
    )

    # status no Telegram SEMPRE a cada N scans
    if scan_count % STATUS_EVERY_SCANS == 0:
        send(
            f"🧾 Status CRYPTO | {fmt_time(now_ts())}\n"
            f"active={c_active} has_price={c_has_price} ready={c_ready} cand={len(candidates)}\n"
            f"trigP={trig_p} trigV={trig_v}"
        )

    if not candidates:
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    top = candidates[:MAX_ALERTS_PER_SCAN]

    send(f"🔔 CRYPTO Scan: {len(top)} alerta(s) | {fmt_time(now_ts())}")

    sent_now = 0
    for _, typ, m, payload in top:
        market_id = str(m.get("id", "")).strip()
        if not market_id:
            continue

        if typ == "price":
            oldp, newp, pct, vol, liq = payload
            send(alert_price(m, oldp, newp, pct, vol, liq))
            mark_sent(market_id, "price")
            sent_now += 1
        else:
            oldv, newv, dv, price, liq = payload
            send(alert_volume(m, oldv, newv, dv, price, liq))
            mark_sent(market_id, "volume")
            sent_now += 1

    alert_count += sent_now


# =========================
# MAIN
# =========================
def main():
    global crypto_tag_id

    send("🔎 Iniciando... buscando tag CRYPTO.")
    crypto_tag_id = fetch_tag_id_by_slug(CRYPTO_TAG_SLUG)

    send(
        "🤖 Bot ligado (CRYPTO ATIVO / agressivo)\n"
        f"tag_slug={CRYPTO_TAG_SLUG} | tag_id={crypto_tag_id}\n"
        f"Scan={SCAN_SECONDS}s | Triggers: preço≥{PRICE_MOVE_PCT*100:.2f}% | volΔ≥{int(VOLUME_JUMP)}\n"
        f"Cooldown: price={COOLDOWN_PRICE_MIN}m | volume={COOLDOWN_VOLUME_MIN}m"
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
