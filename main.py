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
# CONFIG (meio-termo)
# ======================
# filtros de "onde vale olhar"
MIN_VOLUME = float(os.getenv("MIN_VOLUME", "300000"))
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "20000"))
MARKETS_LIMIT = int(os.getenv("MARKETS_LIMIT", "250"))

# thresholds de arbitragem (profit mínimo garantido)
# exemplo: 0.02 = 2% (comprar 1.00 por <= 0.98)
MIN_GUARANTEED_PROFIT = float(os.getenv("MIN_GUARANTEED_PROFIT", "0.02"))

# quantos alerts por ciclo
MAX_ALERTS = int(os.getenv("MAX_ALERTS", "10"))

# intervalo
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "900"))

# anti-spam: não repetir mesma oportunidade por X minutos
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
    """
    Retorna o melhor ASK (menor preço do lado de venda) do orderbook.
    Esse é o preço real pra você COMPRAR.
    """
    if not token_id:
        return 0.0
    url = f"{CLOB_BASE}/book?token_id={token_id}"
    data = await fetch_json(session, url)
    asks = data.get("asks", [])
    if not asks:
        return 0.0
    # asks vem como lista de {price, size} ou arrays dependendo do formato
    top = asks[0]
    if isinstance(top, dict):
        return to_float(top.get("price"))
    if isinstance(top, list) and len(top) > 0:
        return to_float(top[0])
    return 0.0

def get_yes_no_token_ids(m: dict):
    """
    Gamma retorna clobTokenIds (normalmente alinhado com outcomes/outcomePrices).
    Vamos tentar mapear outcomes => tokenId.
    """
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
# Arb detectors
# ======================
BY_DATE_RE = re.compile(r"\b(by|before)\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b", re.IGNORECASE)

def normalize_base_question(q: str) -> str:
    """Remove o 'by/before <date>' pra agrupar ladders."""
    q2 = BY_DATE_RE.sub("", q or "").strip()
    q2 = re.sub(r"\s+", " ", q2)
    return q2.lower()

def parse_date_from_question(q: str):
    m = BY_DATE_RE.search(q or "")
    if not m:
        return None
    raw = m.group(2).replace(",", "").strip()
    # tenta formatos comuns: "March 31 2026"
    try:
        return datetime.strptime(raw, "%B %d %Y")
    except Exception:
        return None

# ======================
# Anti-spam memory
# ======================
seen = {}  # key -> timestamp (epoch)
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
# Main scan
# ======================
async def scan_arbs():
    cleanup_seen()

    async with aiohttp.ClientSession() as session:
        # 1) pega mercados
        markets_url = f"{GAMMA_BASE}/markets?active=true&closed=false&limit={MARKETS_LIMIT}"
        markets = await fetch_json(session, markets_url)
        if not isinstance(markets, list):
            markets = markets.get("markets", [])

        # filtra candidatos
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

        alerts = []

        # =========================
        # A) SAME-MARKET ARB
        # =========================
        for m in candidates:
            # best ask pra comprar YES e NO
            yes_ask = await get_best_ask(session, m["yes_tid"])
            no_ask  = await get_best_ask(session, m["no_tid"])
            if yes_ask <= 0 or no_ask <= 0:
                continue

            cost = yes_ask + no_ask
            guaranteed_profit = 1.0 - cost

            if guaranteed_profit >= MIN_GUARANTEED_PROFIT:
                key = f"same:{m['slug']}:{round(cost,4)}"
                if seen_recently(key):
                    continue
                alerts.append({
                    "type": "SAME_MARKET",
                    "question": m["question"],
                    "slug": m["slug"],
                    "vol": m["volume"],
                    "liq": m["liquidity"],
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "cost": cost,
                    "guaranteed_profit": guaranteed_profit
                })
                mark_seen(key)

            if len(alerts) >= MAX_ALERTS:
                break

        if len(alerts) < MAX_ALERTS:
            # =========================
            # B) DATE-LADDER ARB
            #    Buy NO (earlier) + Buy YES (later) < 1
            # =========================
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
                items.sort(key=lambda x: x[0])  # por data

                # só checa pares (i<j): NO no curto + YES no longo
                for i in range(len(items)-1):
                    dt_i, m_i = items[i]
                    no_ask_i = await get_best_ask(session, m_i["no_tid"])
                    if no_ask_i <= 0:
                        continue

                    for j in range(i+1, len(items)):
                        dt_j, m_j = items[j]
                        yes_ask_j = await get_best_ask(session, m_j["yes_tid"])
                        if yes_ask_j <= 0:
                            continue

                        cost = no_ask_i + yes_ask_j
                        guaranteed_profit = 1.0 - cost

                        if guaranteed_profit >= MIN_GUARANTEED_PROFIT:
                            key = f"ladder:{m_i['slug']}:{m_j['slug']}:{round(cost,4)}"
                            if seen_recently(key):
                                continue
                            alerts.append({
                                "type": "DATE_LADDER",
                                "base": base,
                                "short_q": m_i["question"],
                                "short_slug": m_i["slug"],
                                "long_q": m_j["question"],
                                "long_slug": m_j["slug"],
                                "short_no_ask": no_ask_i,
                                "long_yes_ask": yes_ask_j,
                                "cost": cost,
                                "guaranteed_profit": guaranteed_profit,
                                "vol": min(m_i["volume"], m_j["volume"]),
                                "liq": min(m_i["liquidity"], m_j["liquidity"]),
                            })
                            mark_seen(key)

                        if len(alerts) >= MAX_ALERTS:
                            break
                    if len(alerts) >= MAX_ALERTS:
                        break
                if len(alerts) >= MAX_ALERTS:
                    break

        return alerts

def format_alert(a: dict) -> str:
    gp = a["guaranteed_profit"]
    cost = a["cost"]
    gp_pct = round(gp * 100, 2)
    cost_pct = round(cost * 100, 2)

    if a["type"] == "SAME_MARKET":
        return (
            f"💸 <b>ARB: YES+NO &lt; 1</b>\n"
            f"📊 {a['question']}\n"
            f"YES ask: {round(a['yes_ask'], 4)} | NO ask: {round(a['no_ask'], 4)}\n"
            f"Total cost: {round(cost,4)} ({cost_pct}¢ por $1)\n"
            f"✅ Profit mínimo: {round(gp,4)} ({gp_pct}%)\n"
            f"Vol: {int(a['vol'])} | Liq: {int(a['liq'])}\n"
            f"https://polymarket.com/market/{a['slug']}"
        )

    # DATE_LADDER
    return (
        f"💸 <b>ARB: DATE-LADDER (NO curto + YES longo)</b>\n"
        f"🧩 Base: {a.get('base','')}\n\n"
        f"1) <b>BUY NO</b> (curto): {a['short_q']}\n"
        f"   ask(NO): {round(a['short_no_ask'],4)}\n"
        f"   https://polymarket.com/market/{a['short_slug']}\n\n"
        f"2) <b>BUY YES</b> (longo): {a['long_q']}\n"
        f"   ask(YES): {round(a['long_yes_ask'],4)}\n"
        f"   https://polymarket.com/market/{a['long_slug']}\n\n"
        f"Total cost: {round(cost,4)} ({cost_pct}¢ por $1)\n"
        f"✅ Profit mínimo: {round(gp,4)} ({gp_pct}%)\n"
        f"Vol(min): {int(a['vol'])} | Liq(min): {int(a['liq'])}"
    )

async def main():
    enviar("🤖 ArbBot ligado: procurando arbs (YES+NO<1 e date-ladders).")
    while True:
        try:
            alerts = await scan_arbs()
            if alerts:
                enviar(f"🚨 <b>ARBS encontrados:</b> {len(alerts)}")
                for a in alerts[:MAX_ALERTS]:
                    enviar(format_alert(a))
            else:
                enviar("🤖 Nenhum arb acima do threshold agora.")
        except Exception as e:
            enviar(f"❌ Erro no scan:\n<code>{e}</code>")
            print("Erro:", repr(e))

        await asyncio.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())
