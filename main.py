import os
import re
import asyncio
import aiohttp
import telebot
from datetime import datetime

# ======================
# ENV
# ======================
BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não encontrado no runtime.")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID não encontrado no runtime.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def enviar(msg: str):
    try:
        bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)
    except Exception as e:
        print("Falha ao enviar:", repr(e))

# ======================
# CONFIG (meio-termo + 2 níveis)
# ======================
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "300000"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "20000"))
MARKETS_LIMIT = int(os.getenv("MARKETS_LIMIT", "250"))

ALERT_PROFIT = float(os.getenv("ALERT_PROFIT", "0.02"))      # 2%
WATCH_PROFIT = float(os.getenv("WATCH_PROFIT", "0.01"))      # 1%

MAX_ALERTS = int(os.getenv("MAX_ALERTS", "10"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "900"))
SEEN_TTL_MIN = int(os.getenv("SEEN_TTL_MIN", "120"))

# ======================
# HTTP helpers
# ======================
def to_float(x) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float)):
            return float(x)
        s = str(x).strip().replace(",", "")
        if not s:
            return 0.0
        return float(s)
    except Exception:
        return 0.0

async def fetch_json(session: aiohttp.ClientSession, url: str):
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        resp.raise_for_status()
        return await resp.json()

# ======================
# CLOB pricing (best ask)
# ======================
CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

async def get_best_ask(session: aiohttp.ClientSession, token_id: str) -> float:
    """Melhor ASK = preço real para COMPRAR agora."""
    if not token_id:
        return 0.0
    url = f"{CLOB_BASE}/book?token_id={token_id}"
    data = await fetch_json(session, url)
    asks = data.get("asks", [])
    if not asks:
        return 0.0
    top = asks[0]
    if isinstance(top, dict):
        return to_float(top.get("price"))
    if isinstance(top, list) and len(top) > 0:
        return to_float(top[0])
    return 0.0

def get_yes_no_token_ids(m: dict):
    outcomes = m.get("outcomes")
    token_ids = m.get("clobTokenIds")
    if not isinstance(outcomes, list) or not isinstance(token_ids, list):
        return ("", "")
    if len(outcomes) != len(token_ids):
        return ("", "")

    yes_id = ""
    no_id = ""
    for o, tid in zip(outcomes, token_ids):
        o_norm = str(o).strip().lower()
        if o_norm in ("yes", "true", "y"):
            yes_id = str(tid)
        elif o_norm in ("no", "false", "n"):
            no_id = str(tid)
    return (yes_id, no_id)

# ======================
# Date ladder helpers
# ======================
BY_DATE_RE = re.compile(r"\b(by|before)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b", re.IGNORECASE)

def normalize_base_question(q: str) -> str:
    q2 = BY_DATE_RE.sub("", q or "").strip()
    q2 = re.sub(r"\s+", " ", q2)
    return q2.lower()

def parse_date_from_question(q: str):
    m = BY_DATE_RE.search(q or "")
    if not m:
        return None
    raw = m.group(2).replace(",", "").strip()
    try:
        return datetime.strptime(raw, "%B %d %Y")
    except Exception:
        return None

# ======================
# Anti-spam memory
# ======================
seen = {}  # key -> epoch ts

def now_ts():
    return int(datetime.utcnow().timestamp())

def seen_recently(key: str) -> bool:
    ts = seen.get(key)
    if not ts:
        return False
    return (now_ts() - ts) < (SEEN_TTL_MIN * 60)

def mark_seen(key: str):
    seen[key] = now_ts()

def cleanup_seen():
    cutoff = now_ts() - (SEEN_TTL_MIN * 60)
    for k in list(seen.keys()):
        if seen[k] < cutoff:
            del seen[k]

# ======================
# Arb detectors
# ======================
def arb_level(guaranteed_profit: float) -> str:
    """
    Decide se vira ALERTA / WATCHLIST / IGNORAR.
    """
    if guaranteed_profit >= ALERT_PROFIT:
        return "ALERTA"
    if guaranteed_profit >= WATCH_PROFIT:
        return "WATCHLIST"
    return "IGNORAR"

def fmt_money(x: float) -> str:
    # 0.94 -> "0.9400"
    return f"{x:.4f}"

def fmt_pct(x: float) -> str:
    # 0.06 -> "6.00%"
    return f"{x*100:.2f}%"

# ======================
# Scan
# ======================
async def scan_arbs():
    cleanup_seen()

    async with aiohttp.ClientSession() as session:
        markets_url = f"{GAMMA_BASE}/markets?active=true&closed=false&limit={MARKETS_LIMIT}"
        markets = await fetch_json(session, markets_url)
        if not isinstance(markets, list):
            markets = markets.get("markets", [])

        candidates = []
        for m in markets:
            q = (m.get("question") or "").strip()
            slug = (m.get("slug") or "").strip()
            vol = to_float(m.get("volume"))
            liq = to_float(m.get("liquidity"))
            if not q or not slug:
                continue
            if vol < MIN_VOLUME or liq < MIN_LIQUIDITY:
                continue
            yes_tid, no_tid = get_yes_no_token_ids(m)
            if not yes_tid or not no_tid:
                continue
            candidates.append({
                "question": q,
                "slug": slug,
                "volume": vol,
                "liquidity": liq,
                "yes_tid": yes_tid,
                "no_tid": no_tid,
            })

        results = []

        # A) SAME-MARKET ARB: buy YES + buy NO < 1
        for m in candidates:
            yes_ask = await get_best_ask(session, m["yes_tid"])
            no_ask  = await get_best_ask(session, m["no_tid"])
            if yes_ask <= 0 or no_ask <= 0:
                continue

            cost = yes_ask + no_ask
            gp = 1.0 - cost
            level = arb_level(gp)
            if level == "IGNORAR":
                continue

            key = f"same:{m['slug']}:{round(cost,4)}:{level}"
            if seen_recently(key):
                continue
            mark_seen(key)

            results.append({
                "level": level,
                "type": "SAME_MARKET",
                "question": m["question"],
                "slug": m["slug"],
                "vol": m["volume"],
                "liq": m["liquidity"],
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "cost": cost,
                "gp": gp
            })

            if len(results) >= MAX_ALERTS:
                break

        if len(results) < MAX_ALERTS:
            # B) DATE-LADDER: buy NO (curto) + buy YES (longo) < 1
            ladders = {}
            for m in candidates:
                dt = parse_date_from_question(m["question"])
                if not dt:
                    continue
                base = normalize_base_question(m["question"])
                ladders.setdefault(base, []).append((dt, m))

            for base, items in ladders.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x[0])

                for i in range(len(items) - 1):
                    _, m_short = items[i]
                    no_ask_short = await get_best_ask(session, m_short["no_tid"])
                    if no_ask_short <= 0:
                        continue

                    for j in range(i + 1, len(items)):
                        _, m_long = items[j]
                        yes_ask_long = await get_best_ask(session, m_long["yes_tid"])
                        if yes_ask_long <= 0:
                            continue

                        cost = no_ask_short + yes_ask_long
                        gp = 1.0 - cost
                        level = arb_level(gp)
                        if level == "IGNORAR":
                            continue

                        key = f"ladder:{m_short['slug']}:{m_long['slug']}:{round(cost,4)}:{level}"
                        if seen_recently(key):
                            continue
                        mark_seen(key)

                        results.append({
                            "level": level,
                            "type": "DATE_LADDER",
                            "base": base,
                            "short_q": m_short["question"],
                            "short_slug": m_short["slug"],
                            "long_q": m_long["question"],
                            "long_slug": m_long["slug"],
                            "short_no_ask": no_ask_short,
                            "long_yes_ask": yes_ask_long,
                            "cost": cost,
                            "gp": gp,
                            "vol": min(m_short["volume"], m_long["volume"]),
                            "liq": min(m_short["liquidity"], m_long["liquidity"]),
                        })

                        if len(results) >= MAX_ALERTS:
                            break
                    if len(results) >= MAX_ALERTS:
                        break
                if len(results) >= MAX_ALERTS:
                    break

        # Ordena priorizando ALERTA e maior profit
        results.sort(key=lambda r: (1 if r["level"] == "ALERTA" else 0, r["gp"]), reverse=True)
        return results[:MAX_ALERTS]

def format_result(r: dict) -> str:
    tag = "🚨 <b>ALERTA</b>" if r["level"] == "ALERTA" else "👀 <b>WATCHLIST</b>"
    cost = r["cost"]
    gp = r["gp"]

    if r["type"] == "SAME_MARKET":
        return (
            f"{tag} — <b>ARB YES+NO</b>\n"
            f"📊 {r['question']}\n"
            f"BUY YES @ {fmt_money(r['yes_ask'])} | BUY NO @ {fmt_money(r['no_ask'])}\n"
            f"Total cost: {fmt_money(cost)}  | Profit: {fmt_money(gp)} ({fmt_pct(gp)})\n"
            f"Vol: {int(r['vol'])} | Liq: {int(r['liq'])}\n"
            f"https://polymarket.com/market/{r['slug']}"
        )

    # DATE_LADDER
    return (
        f"{tag} — <b>ARB DATE-LADDER</b>\n"
        f"🧩 Base: {r.get('base','')}\n\n"
        f"1) <b>BUY NO</b> (curto) @ {fmt_money(r['short_no_ask'])}\n"
        f"   {r['short_q']}\n"
        f"   https://polymarket.com/market/{r['short_slug']}\n\n"
        f"2) <b>BUY YES</b> (longo) @ {fmt_money(r['long_yes_ask'])}\n"
        f"   {r['long_q']}\n"
        f"   https://polymarket.com/market/{r['long_slug']}\n\n"
        f"Total cost: {fmt_money(cost)}  | Profit: {fmt_money(gp)} ({fmt_pct(gp)})\n"
        f"Vol(min): {int(r['vol'])} | Liq(min): {int(r['liq'])}"
    )

# ======================
# LOOP
# ======================
async def main():
    enviar("🤖 ArbBot ligado (2 níveis): ALERTA ≥2% | WATCHLIST 1–2%")
    while True:
        try:
            res = await scan_arbs()
            if not res:
                enviar("🤖 Nenhum arb ≥1% agora. (Watch/Alert)")
            else:
                # Envia um resumo e depois os detalhes
                n_alert = sum(1 for x in res if x["level"] == "ALERTA")
                n_watch = sum(1 for x in res if x["level"] == "WATCHLIST")
                enviar(f"💡 Encontrados: 🚨 {n_alert} ALERTA | 👀 {n_watch} WATCHLIST")

                for r in res:
                    enviar(format_result(r))

        except Exception as e:
            enviar(f"❌ Erro no scan:\n<code>{e}</code>")
            print("Erro:", repr(e))

        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
