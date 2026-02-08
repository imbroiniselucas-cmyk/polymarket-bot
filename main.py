#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# =========================
# REQUIRED ENV (precisa existir)
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

# =========================
# HARD SETTINGS (sem mexer em Variables)
# =========================
POLL_SECONDS = 35                 # mais agressivo
MAX_ALERTS_PER_CYCLE = 70         # manda bastante
PAGE_LIMIT = 250
MAX_PAGES = 8                     # ~2000 mercados/ciclo (8*250)
HEARTBEAT_EVERY_SEC = 180         # 3 min

# ===== Repetição / anti-spam (inteligente) =====
# Não repete o mesmo mercado/tipo se nada mudou.
MARKET_COOLDOWN_SEC = 8 * 60      # 8 min por mercado/tipo (se não mudar)
PRICE_DELTA_RESEND = 0.015        # reenviar se preço mudou >= 1.5 cent
SCORE_DELTA_RESEND = 8.0          # reenviar se score mudou >= 8 pontos
MIN_SECONDS_BETWEEN_ANY_SEND = 1  # evita 429

# ===== Estratégia (mais flexível/agressiva) =====
# Arbitragem: aceita micro gaps
ARB_GAP_MIN_NET = 0.0025          # 0.25% líquido (bem agressivo)
FEE_BUFFER = 0.0015               # 0.15% buffer

# Cheap: mais amplo
CHEAP_MAX_PRICE = 0.18            # barato até 18 cents

# Mispricing (sem dados externos):
# dispara quando um lado está MUITO barato, mas o mercado tem volume/liq (evita mercado morto)
MISPRICE_MAX_PRICE = 0.25         # lado <= 25 cents
MISPRICE_MIN_LIQ = 3000           # liquidez mínima
MISPRICE_MIN_VOL24 = 1500         # volume 24h mínimo

# Sanity/spread proxy: penaliza dados estranhos, mas não bloqueia tudo
SUM_ERR_SOFT_SKIP = 0.28          # se abs(yes+no-1) > isso, ignora (salvo arb forte)
SUM_ERR_PEN_W = 45.0

# Score: não filtra por padrão (pode ajustar aqui)
SCORE_MIN = 0.0

# =========================
# UTIL
# =========================
def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

def safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def clamp01(x):
    if x is None:
        return None
    return clamp(x, 0.0, 1.0)

# =========================
# HTTP (stdlib)
# =========================
def http_get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body)

def http_post_json(url: str, payload: dict, timeout: int = 20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        return e.code, body
    except Exception as e:
        return 0, repr(e)

# =========================
# TELEGRAM
# =========================
_last_send_ts = 0

def tg_send(text: str) -> bool:
    global _last_send_ts

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID (env vars).")
        return False

    # pacing simples para não bater 429
    now = time.time()
    if now - _last_send_ts < MIN_SECONDS_BETWEEN_ANY_SEND:
        time.sleep(MIN_SECONDS_BETWEEN_ANY_SEND - (now - _last_send_ts))

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}

    status, body = http_post_json(url, payload, timeout=20)
    _last_send_ts = time.time()

    if status == 200:
        return True

    # 429 rate limit retry
    if status == 429 and "retry_after" in body:
        retry_after = 2
        try:
            data = json.loads(body)
            retry_after = int(data.get("parameters", {}).get("retry_after", 2))
        except Exception:
            pass
        print(f"⚠️ Telegram 429. Sleep {retry_after}s then retry.")
        time.sleep(retry_after)
        status2, body2 = http_post_json(url, payload, timeout=20)
        _last_send_ts = time.time()
        if status2 == 200:
            return True
        print("❌ Telegram send failed after retry:", status2, body2[:300])
        return False

    print("❌ Telegram send failed:", status, body[:300])
    return False

# =========================
# GAMMA
# =========================
def gamma_get(path: str, params: dict, timeout: int = 25):
    qs = urllib.parse.urlencode(params)
    url = f"{GAMMA_URL}{path}?{qs}"
    return http_get_json(url, timeout=timeout)

def extract_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "markets", "results"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []

def fetch_markets_paged():
    all_markets = []
    used = "/markets"
    last_err = None

    for page in range(MAX_PAGES):
        offset = page * PAGE_LIMIT
        try:
            data = gamma_get("/markets", {
                "active": "true",
                "closed": "false",
                "limit": str(PAGE_LIMIT),
                "offset": str(offset),
                "order": "volume24hr",
                "ascending": "false",
            }, timeout=25)
            lst = extract_list(data)
            if not lst:
                break
            all_markets.extend(lst)
        except Exception as e:
            last_err = repr(e)
            break

    # fallback events
    if not all_markets:
        try:
            used = "/events"
            data = gamma_get("/events", {
                "active": "true",
                "closed": "false",
                "limit": "200",
                "offset": "0",
            }, timeout=25)
            evs = extract_list(data)
            mkts = []
            for ev in evs:
                if isinstance(ev, dict) and isinstance(ev.get("markets"), list):
                    mkts.extend(ev["markets"])
            if mkts:
                return mkts, None, used
        except Exception as e:
            last_err = repr(e)

    return all_markets, last_err, used

def market_url(market: dict):
    slug = market.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = market.get("id") or market.get("conditionId") or market.get("condition_id")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

def get_liq_vol(market: dict):
    liq = safe_float(market.get("liquidity") or market.get("liquidityNum") or market.get("liquidity_num"), 0.0) or 0.0
    vol24 = safe_float(market.get("volume24hr") or market.get("volume24h") or market.get("volumeNum") or market.get("volume_num"), 0.0) or 0.0
    return liq, vol24

# =========================
# PARSE PRICES (robusto)
# =========================
def parse_outcome_prices(value):
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                return arr if isinstance(arr, list) else None
            except Exception:
                return None
        if "," in s:
            return [p.strip().strip('"').strip("'") for p in s.split(",")]
    return None

def parse_yes_no(market: dict):
    yes = None
    no = None

    op_raw = market.get("outcomePrices") or market.get("outcome_prices")
    op = parse_outcome_prices(op_raw)
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no = safe_float(op[1], None)

    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no = safe_float(toks[1].get("price"), no)

    if yes is None:
        lp = market.get("lastTradePrice") or market.get("lastPrice") or market.get("last_price")
        yes = safe_float(lp, None)
        if yes is not None and no is None:
            no = 1.0 - yes

    yes = clamp01(yes)
    no = clamp01(no)
    if yes is None or no is None:
        return None, None
    return yes, no

# =========================
# SIGNALS + SCORE
# =========================
def arb_gap(yes: float, no: float) -> float:
    return 1.0 - (yes + no)

def cheap_side(yes: float, no: float):
    # returns (header, side_price, cheapness 0..1)
    if yes <= CHEAP_MAX_PRICE:
        return "CHEAP (BUY YES)", yes, (CHEAP_MAX_PRICE - yes) / max(CHEAP_MAX_PRICE, 1e-9)
    if no <= CHEAP_MAX_PRICE:
        return "CHEAP (BUY NO)", no, (CHEAP_MAX_PRICE - no) / max(CHEAP_MAX_PRICE, 1e-9)
    return None, None, 0.0

def misprice_side(yes: float, no: float, liq: float, vol24: float):
    # mais flexível: lado <= 0.25, mas exige mercado “vivo”
    if liq < MISPRICE_MIN_LIQ or vol24 < MISPRICE_MIN_VOL24:
        return None, None, 0.0

    # define “mispricing” como lado muito barato, porém não tão extremo quanto CHEAP
    if yes <= MISPRICE_MAX_PRICE:
        # quanto mais barato, maior
        return "MISPRICING (BUY YES)", yes, (MISPRICE_MAX_PRICE - yes) / max(MISPRICE_MAX_PRICE, 1e-9)
    if no <= MISPRICE_MAX_PRICE:
        return "MISPRICING (BUY NO)", no, (MISPRICE_MAX_PRICE - no) / max(MISPRICE_MAX_PRICE, 1e-9)
    return None, None, 0.0

def score(net_gap: float, cheapness: float, liq: float, vol24: float, sum_err: float) -> float:
    # normalize gap: 0..5%
    gap_n = clamp(net_gap / 0.05, 0.0, 1.0)
    liq_n = clamp(math.log10(liq + 1.0) / 5.0, 0.0, 1.0)
    vol_n = clamp(math.log10(vol24 + 1.0) / 6.0, 0.0, 1.0)

    base = (62.0 * gap_n) + (22.0 * clamp(cheapness, 0.0, 1.0)) + (10.0 * liq_n) + (6.0 * vol_n)
    pen = min(SUM_ERR_PEN_W * sum_err, 45.0)
    return round(clamp(base - pen, 0.0, 100.0), 1)

def format_msg(market: dict, header: str, sc: float, yes: float, no: float, liq: float, vol24: float, details: str):
    title = (market.get("question") or market.get("title") or "Market").strip()
    url = market_url(market)
    return (
        f"🚨 {header} | Score {sc}\n"
        f"🧠 {title}\n"
        f"💰 YES {yes:.3f} | NO {no:.3f}\n"
        f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
        f"📝 {details}\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc()}"
    )

# =========================
# DEDUPE INTELIGENTE (para não repetir)
# =========================
# key -> {ts, yes, no, score}
sent_state = {}

def should_send_smart(key: str, yes: float, no: float, sc: float) -> bool:
    """
    Regra:
    - Se nunca enviou: envia
    - Se passou cooldown (8 min): envia
    - OU se preço mudou >= 1.5c
    - OU score mudou >= 8
    """
    now = time.time()
    prev = sent_state.get(key)

    if prev is None:
        sent_state[key] = {"ts": now, "yes": yes, "no": no, "score": sc}
        return True

    dt = now - prev["ts"]
    if dt >= MARKET_COOLDOWN_SEC:
        sent_state[key] = {"ts": now, "yes": yes, "no": no, "score": sc}
        return True

    if abs(yes - prev["yes"]) >= PRICE_DELTA_RESEND or abs(no - prev["no"]) >= PRICE_DELTA_RESEND:
        sent_state[key] = {"ts": now, "yes": yes, "no": no, "score": sc}
        return True

    if abs(sc - prev["score"]) >= SCORE_DELTA_RESEND:
        sent_state[key] = {"ts": now, "yes": yes, "no": no, "score": sc}
        return True

    return False

def make_key(market: dict, kind: str):
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    return f"{mid}:{kind}"

# =========================
# MAIN
# =========================
def boot():
    print("BOOT_OK: main.py running")
    tg_send(
        "✅ Bot ON | agressivo + flexível | sem alertas repetidos\n"
        f"poll={POLL_SECONDS}s | scan≈{MAX_PAGES*PAGE_LIMIT} mercados/ciclo | max/cycle={MAX_ALERTS_PER_CYCLE}\n"
        f"arb_net≥{ARB_GAP_MIN_NET*100:.2f}% (buffer {FEE_BUFFER*100:.2f}%) | cheap≤{CHEAP_MAX_PRICE:.2f} | misprice≤{MISPRICE_MAX_PRICE:.2f}\n"
        f"🕒 {now_utc()}"
    )

def main():
    boot()
    last_heartbeat = 0

    while True:
        markets, err, used = fetch_markets_paged()
        if not markets:
            tg_send(f"⚠️ Gamma 0 mercados.\nEndpoint: {used}\nErro: {err}\n🕒 {now_utc()}")
            time.sleep(POLL_SECONDS)
            continue

        parse_ok = 0
        candidates = []

        for m in markets:
            yes, no = parse_yes_no(m)
            if yes is None:
                continue
            parse_ok += 1

            liq, vol24 = get_liq_vol(m)
            sum_err = abs((yes + no) - 1.0)

            gap = arb_gap(yes, no)
            net_gap = gap - FEE_BUFFER

            # se dados MUITO esquisitos e sem arb forte, ignora
            if sum_err > SUM_ERR_SOFT_SKIP and net_gap < (ARB_GAP_MIN_NET * 2):
                continue

            # 1) ARBITRAGEM
            if net_gap >= ARB_GAP_MIN_NET:
                # cheapness ajuda score se um lado também estiver baixo
                _, _, cheapness = cheap_side(yes, no)
                sc = score(net_gap, cheapness, liq, vol24, sum_err)

                if sc >= SCORE_MIN:
                    details = (
                        f"AÇÃO: BUY YES + BUY NO | YES+NO={yes+no:.3f} | "
                        f"gap bruto={gap*100:.2f}% | gap líquido≈{net_gap*100:.2f}% | spread_proxy={sum_err:.3f}"
                    )
                    key = make_key(m, "ARB")
                    if should_send_smart(key, yes, no, sc):
                        candidates.append((sc, m, "ARBITRAGEM (BUY YES + BUY NO)", yes, no, liq, vol24, details))
                continue

            # 2) CHEAP
            header, side_price, cheapness = cheap_side(yes, no)
            if header:
                sc = score(0.0, cheapness, liq, vol24, sum_err)
                if sc >= SCORE_MIN:
                    details = f"AÇÃO: {header.replace('CHEAP ', '')} | preço={side_price:.3f} | spread_proxy={sum_err:.3f}"
                    key = make_key(m, header)
                    if should_send_smart(key, yes, no, sc):
                        candidates.append((sc, m, header, yes, no, liq, vol24, details))
                continue

            # 3) MISPRICING (flexível)
            header2, side_price2, misp = misprice_side(yes, no, liq, vol24)
            if header2:
                # mispricing conta como “cheapness leve”
                sc = score(0.0, 0.65 * misp, liq, vol24, sum_err)
                if sc >= SCORE_MIN:
                    details = (
                        f"AÇÃO: {header2.split(' (')[1].replace(')', '')} | preço={side_price2:.3f} | "
                        f"liq={int(liq)} vol24={int(vol24)} | spread_proxy={sum_err:.3f}"
                    )
                    key = make_key(m, header2)
                    if should_send_smart(key, yes, no, sc):
                        candidates.append((sc, m, header2, yes, no, liq, vol24, details))

        # manda melhores primeiro
        candidates.sort(key=lambda x: x[0], reverse=True)

        sent = 0
        for sc, m, header, yes, no, liq, vol24, details in candidates[:MAX_ALERTS_PER_CYCLE]:
            if tg_send(format_msg(m, header, sc, yes, no, liq, vol24, details)):
                sent += 1

        print(f"[{now_utc()}] markets={len(markets)} parse_ok={parse_ok} candidates={len(candidates)} sent={sent} endpoint={used}")

        # heartbeat
        now = time.time()
        if sent == 0 and (now - last_heartbeat) >= HEARTBEAT_EVERY_SEC:
            tg_send(
                "💓 HEARTBEAT: scan OK (sem oportunidades novas / sem mudanças relevantes)\n"
                f"markets={len(markets)} | parse_ok={parse_ok} | candidates={len(candidates)} | sent={sent}\n"
                f"endpoint={used} | cooldown_market={MARKET_COOLDOWN_SEC}s | priceΔ={PRICE_DELTA_RESEND:.3f} | scoreΔ={SCORE_DELTA_RESEND}\n"
                f"🕒 {now_utc()}"
            )
            last_heartbeat = now

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
