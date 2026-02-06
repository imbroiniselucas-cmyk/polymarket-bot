#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import math
import requests
from datetime import datetime, timezone

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
GAMMA_URL = os.getenv("GAMMA_URL", "https://gamma-api.polymarket.com").rstrip("/")

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))                  # 1 min
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "60"))  # agressivo
REPEAT_COOLDOWN_SEC = int(os.getenv("REPEAT_COOLDOWN_SEC", "30"))    # repetir rápido

PAGE_LIMIT = int(os.getenv("PAGE_LIMIT", "250"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "6"))  # 6*250=1500 mercados

SCORE_MIN = float(os.getenv("SCORE_MIN", "0"))  # 0 = não filtra; 30 = só >=30

REPLAY_ON_BOOT = os.getenv("REPLAY_ON_BOOT", "0").strip() == "1"
REPLAY_LIMIT = int(os.getenv("REPLAY_LIMIT", "20"))

HISTORY_PATH = os.getenv("HISTORY_PATH", "sent_history.jsonl")  # json lines
DEBUG_EVERY_SEC = int(os.getenv("DEBUG_EVERY_SEC", "180"))       # 3 min

# =========================
# HELPERS
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

def tg_api(method: str, payload=None, timeout=20):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}"
    r = requests.post(url, json=payload or {}, timeout=timeout)
    return r.status_code, r.text

def tg_send(text: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID.")
        return False

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }

    status, body = tg_api("sendMessage", payload)

    if status == 200:
        return True

    # rate limit (429)
    if status == 429:
        retry_after = 2
        try:
            if "retry_after" in body:
                import re
                m = re.search(r"retry_after\":\s*(\d+)", body)
                if m:
                    retry_after = int(m.group(1))
        except Exception:
            pass

        print(f"⚠️ Telegram 429. Sleep {retry_after}s and retry.")
        time.sleep(retry_after)
        status2, body2 = tg_api("sendMessage", payload)
        if status2 == 200:
            return True
        print("❌ Telegram failed after retry:", status2, body2[:400])
        return False

    print("❌ Telegram sendMessage failed:", status, body[:400])
    return False

def gamma_get(path, params=None, timeout=25):
    url = GAMMA_URL + path
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()

def extract_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "markets", "results"):
            if isinstance(payload.get(k), list):
                return payload[k]
    return []

def market_url(market: dict):
    slug = market.get("slug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    mid = market.get("id") or market.get("conditionId") or market.get("condition_id")
    if mid:
        return f"https://polymarket.com/market/{mid}"
    return "https://polymarket.com"

# =========================
# FETCH MARKETS (PAGINADO)
# =========================
def fetch_markets_paged():
    all_markets = []
    used = "/markets"
    last_err = None

    for page in range(MAX_PAGES):
        offset = page * PAGE_LIMIT
        try:
            data = gamma_get("/markets", params={
                "active": "true",
                "closed": "false",
                "limit": str(PAGE_LIMIT),
                "offset": str(offset),
                "order": "volume24hr",
                "ascending": "false",
            })
            lst = extract_list(data)
            if not lst:
                break
            all_markets.extend(lst)
        except Exception as e:
            last_err = repr(e)
            break

    # fallback: /events
    if not all_markets:
        try:
            used = "/events"
            data = gamma_get("/events", params={
                "active": "true",
                "closed": "false",
                "limit": "200",
                "offset": "0",
            })
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

# =========================
# PARSE YES/NO (CORRIGIDO)
# =========================
def parse_outcome_prices(value):
    """
    value pode ser:
    - list: ["0.2","0.8"] ou [0.2, 0.8]
    - string JSON: "[\"0.2\",\"0.8\"]"
    - string CSV: "0.2,0.8"
    """
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
            parts = [p.strip().strip('"').strip("'") for p in s.split(",")]
            return parts
    return None

def parse_yes_no(market: dict):
    yes = None
    no = None

    op_raw = market.get("outcomePrices") or market.get("outcome_prices")
    op = parse_outcome_prices(op_raw)
    if isinstance(op, list) and len(op) >= 2:
        yes = safe_float(op[0], None)
        no  = safe_float(op[1], None)

    # tokens fallback
    if (yes is None or no is None) and isinstance(market.get("tokens"), list):
        toks = market["tokens"]
        if len(toks) >= 2:
            yes = safe_float(toks[0].get("price"), yes)
            no  = safe_float(toks[1].get("price"), no)

    # lastPrice fallback
    if yes is None:
        lp = market.get("lastTradePrice") or market.get("lastPrice") or market.get("last_price")
        yes = safe_float(lp, None)
        if yes is not None and no is None:
            no = 1.0 - yes

    yes = clamp01(yes)
    no  = clamp01(no)
    if yes is None or no is None:
        return None, None
    return yes, no

# =========================
# BUY LOGIC + SCORE
# =========================
def decide_buy(yes: float, no: float):
    # BUY-only: escolhe um lado sempre
    if yes < 0.5:
        return "BUY_YES"
    if yes > 0.5:
        return "BUY_NO"
    return "BUY_YES" if yes <= no else "BUY_NO"

def compute_score(yes: float, liq: float, vol24: float, sum_err: float):
    """
    Score 0-100 estável:
    - favorece mercados com liquidez e volume
    - favorece preços perto de 0.5 (mais tradeável)
    - penaliza dados estranhos (yes+no muito fora de 1)
    """
    mid_pref = 1.0 - abs(yes - 0.5) * 2.0
    mid_pref = clamp(mid_pref, 0.0, 1.0)

    liq_n = clamp(math.log10(liq + 1.0) / 5.0, 0.0, 1.0)     # 1e5 ~ 1
    vol_n = clamp(math.log10(vol24 + 1.0) / 6.0, 0.0, 1.0)   # 1e6 ~ 1

    err_pen = clamp(sum_err * 10.0, 0.0, 0.7)

    score = (40.0 * mid_pref) + (35.0 * liq_n) + (25.0 * vol_n) - (35.0 * err_pen)
    return round(clamp(score, 0.0, 100.0), 1)

def get_liq_vol(market: dict):
    liq = safe_float(
        market.get("liquidity")
        or market.get("liquidityNum")
        or market.get("liquidity_num"),
        0.0
    ) or 0.0
    vol24 = safe_float(
        market.get("volume24hr")
        or market.get("volume24h")
        or market.get("volumeNum")
        or market.get("volume_num"),
        0.0
    ) or 0.0
    return liq, vol24

def format_buy(market: dict, rec: str, yes: float, no: float, score: float, tag: str = "BUY ALERT"):
    title = (market.get("question") or market.get("title") or "Market").strip()
    liq, vol24 = get_liq_vol(market)
    url = market_url(market)

    if rec == "BUY_YES":
        action = "🟢 COMPRA: YES (a favor)"
        alvo = yes
    else:
        action = "🔴 COMPRA: NO (contra)"
        alvo = no

    return (
        f"🚨 {tag} | Score {score}\n"
        f"{action}\n"
        f"🧠 {title}\n"
        f"💰 YES {yes:.3f} | NO {no:.3f} | alvo {alvo:.3f}\n"
        f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
        f"🔗 {url}\n"
        f"🕒 {now_utc()}"
    )

# =========================
# HISTORY (persistente)
# =========================
def append_history(item: dict):
    try:
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception as e:
        print("history append failed:", repr(e))

def load_history_last(n: int):
    """
    Lê os últimos n itens do arquivo jsonl.
    Sem dependências; eficiente o suficiente para n pequeno.
    """
    if n <= 0:
        return []
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        lines = lines[-n:]
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out
    except Exception as e:
        print("history load failed:", repr(e))
        return []

def replay_last_alerts():
    items = load_history_last(REPLAY_LIMIT)
    if not items:
        tg_send(f"🔁 REPLAY: sem histórico disponível (arquivo vazio). Hora {now_utc()}")
        return

    tg_send(f"🔁 REPLAY MODE: repetindo últimos {len(items)} alertas com SCORE. Hora {now_utc()}")

    sent = 0
    for it in items:
        try:
            title = it.get("title") or "Market"
            url = it.get("url") or "https://polymarket.com"
            rec = it.get("rec") or "BUY_YES"
            yes = safe_float(it.get("yes"), None)
            no  = safe_float(it.get("no"), None)
            liq = safe_float(it.get("liq"), 0.0) or 0.0
            vol24 = safe_float(it.get("vol24"), 0.0) or 0.0

            if yes is None or no is None:
                continue

            sum_err = abs((yes + no) - 1.0)
            score = compute_score(yes, liq, vol24, sum_err)

            if score < SCORE_MIN:
                continue

            action = "🟢 COMPRA: YES (a favor)" if rec == "BUY_YES" else "🔴 COMPRA: NO (contra)"
            alvo = yes if rec == "BUY_YES" else no

            msg = (
                f"🚨 REPLAY BUY | Score {score}\n"
                f"{action}\n"
                f"🧠 {title}\n"
                f"💰 YES {yes:.3f} | NO {no:.3f} | alvo {alvo:.3f}\n"
                f"📊 Liq {int(liq)} | Vol24h {int(vol24)}\n"
                f"🔗 {url}\n"
                f"🕒 {now_utc()}"
            )
            if tg_send(msg):
                sent += 1
            time.sleep(0.8)  # evita 429
        except Exception:
            continue

    tg_send(f"✅ REPLAY concluído. enviados={sent}. Hora {now_utc()}")

# =========================
# DEDUPE (repetição controlada)
# =========================
last_sent = {}

def should_send(key: str):
    if REPEAT_COOLDOWN_SEC <= 0:
        return True
    now = time.time()
    ts = last_sent.get(key, 0)
    if now - ts >= REPEAT_COOLDOWN_SEC:
        last_sent[key] = now
        return True
    return False

def make_key(market: dict, rec: str, yes: float, no: float):
    mid = market.get("id") or market.get("conditionId") or market.get("slug") or (market.get("question") or "m")
    price = yes if rec == "BUY_YES" else no
    bucket = round(price, 3)
    return f"{mid}:{rec}:{bucket}"

# =========================
# MAIN
# =========================
def boot_tests():
    print("BOOT_OK: main.py running")
    # confirma telegram rapidamente
    st, body = tg_api("getMe", {})
    if st != 200:
        print("❌ Telegram getMe failed:", st, body[:300])
    else:
        tg_send(
            f"✅ Bot ON | BUY-only | score+replay | poll={POLL_SECONDS}s | pages={MAX_PAGES}x{PAGE_LIMIT} | "
            f"max/cycle={MAX_ALERTS_PER_CYCLE} | cooldown={REPEAT_COOLDOWN_SEC}s | score_min={SCORE_MIN}"
        )

def main():
    boot_tests()

    if REPLAY_ON_BOOT:
        replay_last_alerts()

    last_debug = 0

    while True:
        markets, err, used = fetch_markets_paged()
        if not markets:
            tg_send(f"⚠️ Gamma retornou 0 mercados.\nEndpoint: {used}\nErro: {err}\nHora: {now_utc()}")
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
            score = compute_score(yes, liq, vol24, sum_err)

            if score < SCORE_MIN:
                continue

            rec = decide_buy(yes, no)
            key = make_key(m, rec, yes, no)

            if should_send(key):
                candidates.append((score, m, rec, yes, no, liq, vol24))

        candidates.sort(key=lambda x: x[0], reverse=True)  # melhores scores primeiro

        sent = 0
        for score, m, rec, yes, no, liq, vol24 in candidates[:MAX_ALERTS_PER_CYCLE]:
            msg = format_buy(m, rec, yes, no, score, tag="BUY ALERT")
            ok = tg_send(msg)
            if ok:
                sent += 1
                # salva histórico pro replay
                title = (m.get("question") or m.get("title") or "Market").strip()
                append_history({
                    "ts": now_utc(),
                    "rec": rec,
                    "yes": yes,
                    "no": no,
                    "liq": liq,
                    "vol24": vol24,
                    "title": title,
                    "url": market_url(m),
                })

        print(f"[{now_utc()}] markets={len(markets)} parse_ok={parse_ok} candidates={len(candidates)} sent={sent} used={used}")

        now = time.time()
        if sent == 0 and (now - last_debug) >= DEBUG_EVERY_SEC:
            tg_send(
                "🧩 DEBUG (sem alertas enviados)\n"
                f"markets={len(markets)} | parse_ok={parse_ok} | candidates={len(candidates)} | sent={sent}\n"
                f"endpoint={used} | cooldown={REPEAT_COOLDOWN_SEC}s | max/cycle={MAX_ALERTS_PER_CYCLE}\n"
                f"SCORE_MIN={SCORE_MIN}\n"
                f"Hora: {now_utc()}"
            )
            last_debug = now

        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
