import os
import re
import time
import json
import math
import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# =========================
# CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Varredura
SCAN_EVERY_SECONDS = int(os.getenv("SCAN_EVERY_SECONDS", "120"))  # ex: 2 min
MAX_MARKETS = int(os.getenv("MAX_MARKETS", "250"))                # limita carga
MIN_LIQUIDITY = float(os.getenv("MIN_LIQUIDITY", "2000"))         # filtra cedo
MIN_VOLUME_24H = float(os.getenv("MIN_VOLUME_24H", "5000"))       # filtra cedo

# Oportunidade
MIN_GAP = float(os.getenv("MIN_GAP", "0.06"))                     # 6%+
MIN_SCORE = float(os.getenv("MIN_SCORE", "1.2"))                  # score mínimo

# Anti-spam (não repetir o mesmo mercado toda hora)
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "3600"))     # 1h

# Concurrency
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "15"))
MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "12"))

# =========================
# UTILS
# =========================

@dataclass
class Market:
    id: str
    question: str
    url: str
    category: str
    best_yes: Optional[float]  # prob implícita (ou preço do YES normalizado)
    liquidity: float
    volume_24h: float
    end_time: Optional[str] = None


class TTLCache:
    """Cache simples com TTL (em memória) para evitar repetir chamadas externas."""
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self.store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str):
        v = self.store.get(key)
        if not v:
            return None
        t, data = v
        if time.time() - t > self.ttl:
            self.store.pop(key, None)
            return None
        return data

    def set(self, key: str, value: Any):
        self.store[key] = (time.time(), value)


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def now_ts() -> int:
    return int(time.time())


# =========================
# TELEGRAM
# =========================
async def telegram_send(session: aiohttp.ClientSession, text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        # Se não tiver token/chat_id, só printa
        print(text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True
    }
    async with session.post(url, json=payload, timeout=HTTP_TIMEOUT) as r:
        if r.status >= 400:
            body = await r.text()
            print("Telegram error:", r.status, body)


# =========================
# POLYMARKET FETCH (CLOB-ish)
# =========================
async def fetch_json(session: aiohttp.ClientSession, url: str, params: Optional[dict] = None) -> Any:
    async with session.get(url, params=params, timeout=HTTP_TIMEOUT) as r:
        r.raise_for_status()
        return await r.json()


async def fetch_polymarket_markets(session: aiohttp.ClientSession) -> List[Market]:
    """
    Tenta buscar mercados com filtros básicos.
    Ajuste o endpoint se o seu Polymarket estiver diferente.
    """
    # Endpoint CLOB “markets”
    # (Se falhar, você substitui por outro endpoint que você já estiver usando)
    base = "https://clob.polymarket.com"
    url = f"{base}/markets"

    # Alguns endpoints aceitam "limit" e "next_cursor".
    params = {"limit": MAX_MARKETS}

    data = await fetch_json(session, url, params=params)

    # Normalização defensiva (porque estrutura varia)
    raw_markets = data.get("data") or data.get("markets") or data

    markets: List[Market] = []
    for m in raw_markets[:MAX_MARKETS]:
        q = (m.get("question") or m.get("title") or "").strip()
        if not q:
            continue

        # Liquidez / volume podem vir com nomes diferentes
        liquidity = safe_float(m.get("liquidity") or m.get("liquidityNum") or m.get("liquidityUSD") or 0)
        volume_24h = safe_float(m.get("volume24h") or m.get("volume_24h") or m.get("volume") or 0)

        # Filtros cedo (performance)
        if liquidity < MIN_LIQUIDITY or volume_24h < MIN_VOLUME_24H:
            continue

        mid = str(m.get("market_id") or m.get("id") or "")
        if not mid:
            continue

        slug = m.get("slug") or mid
        url_market = m.get("url") or f"https://polymarket.com/market/{slug}"

        category = (m.get("category") or m.get("categories") or "other")
        if isinstance(category, list):
            category = category[0] if category else "other"
        category = str(category).lower()

        # Prob do YES:
        # Em alguns formatos vem como preço/odd. Aqui tentamos achar algo do tipo "best_bid"/"best_ask" do YES.
        best_yes = None
        # Tenta campos comuns
        best_yes = m.get("best_yes") or m.get("yesPrice") or m.get("probability") or None
        best_yes = safe_float(best_yes, default=None) if best_yes is not None else None
        if best_yes is not None:
            best_yes = clamp01(best_yes)

        end_time = m.get("end_time") or m.get("endTime") or m.get("resolve_time") or None

        markets.append(Market(
            id=mid,
            question=q,
            url=url_market,
            category=category,
            best_yes=best_yes,
            liquidity=liquidity,
            volume_24h=volume_24h,
            end_time=end_time
        ))

    return markets


# =========================
# EXTERNAL FAIR VALUE ESTIMATORS
# =========================
weather_cache = TTLCache(ttl_seconds=15 * 60)  # 15 min cache
crypto_cache = TTLCache(ttl_seconds=60)        # 1 min cache

# Heurísticas simples (você pode evoluir)
WEATHER_PATTERNS = [
    r"\btemperature\b", r"\btemp\b", r"\brain\b", r"\bsnow\b", r"\bwind\b",
    r"\bstorm\b", r"\bhurricane\b", r"\bprecip\b", r"\bweather\b"
]
CLIMATE_PATTERNS = [
    r"\bclimate\b", r"\bco2\b", r"\bemissions\b", r"\bcarbon\b", r"\bwarming\b"
]
CRYPTO_PATTERNS = [
    r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b", r"\bsolana\b", r"\bsol\b"
]


def is_weather_market(q: str) -> bool:
    s = q.lower()
    return any(re.search(p, s) for p in WEATHER_PATTERNS)

def is_climate_market(q: str) -> bool:
    s = q.lower()
    return any(re.search(p, s) for p in CLIMATE_PATTERNS)

def is_crypto_market(q: str) -> bool:
    s = q.lower()
    return any(re.search(p, s) for p in CRYPTO_PATTERNS)


def extract_city_hint(q: str) -> Optional[str]:
    """
    Heurística: tenta pegar algo após 'in ' ou 'at '.
    Melhorar depois com uma lista de cidades/regex.
    """
    s = q.strip()
    m = re.search(r"\b(in|at)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)", s)
    if m:
        return m.group(2)
    return None


async def fair_value_weather(session: aiohttp.ClientSession, market: Market) -> Optional[float]:
    """
    Retorna um 'fair probability' estimado para mercados de clima.
    Sem NLP pesado: usa Open-Meteo só quando consegue inferir cidade.
    Se não der para inferir, retorna None.
    """
    city = extract_city_hint(market.question)
    if not city:
        return None

    cache_key = f"weather:{city}"
    cached = weather_cache.get(cache_key)
    if cached is not None:
        return cached

    # Geocoding via Open-Meteo (grátis)
    geo_url = "https://geocoding-api.open-meteo.com/v1/search"
    geo = await fetch_json(session, geo_url, params={"name": city, "count": 1, "language": "en", "format": "json"})
    results = geo.get("results") or []
    if not results:
        weather_cache.set(cache_key, None)
        return None

    lat = results[0]["latitude"]
    lon = results[0]["longitude"]

    # Forecast básico (7 dias) — exemplo. (Você pode ajustar com base na data do mercado.)
    fc_url = "https://api.open-meteo.com/v1/forecast"
    fc = await fetch_json(session, fc_url, params={
        "latitude": lat, "longitude": lon,
        "daily": "precipitation_probability_max,temperature_2m_max",
        "timezone": "UTC"
    })

    daily = fc.get("daily") or {}
    # Heurística: se a pergunta tiver "rain", usa prob precip. Senão tenta temp.
    q = market.question.lower()

    if "rain" in q or "precip" in q or "snow" in q or "storm" in q:
        probs = daily.get("precipitation_probability_max") or []
        if not probs:
            weather_cache.set(cache_key, None)
            return None
        # usa o maior valor dos próximos dias como proxy (simples)
        fair = clamp01(max(probs) / 100.0)
    elif "temp" in q or "temperature" in q:
        temps = daily.get("temperature_2m_max") or []
        if not temps:
            weather_cache.set(cache_key, None)
            return None
        # Sem o threshold do mercado (ex "above 30C"), não dá para calcular prob real.
        # Proxy: normaliza a temperatura vs faixa (0..40C) (heurístico)
        fair = clamp01((max(temps) - 0.0) / 40.0)
    else:
        weather_cache.set(cache_key, None)
        return None

    weather_cache.set(cache_key, fair)
    return fair


async def fair_value_crypto(session: aiohttp.ClientSession, market: Market) -> Optional[float]:
    """
    Heurística rápida para crypto:
    - Se o mercado menciona "above $X by DATE", você pode estimar fair com volatilidade implícita (mais complexo).
    - Aqui: proxy simples com momentum 24h do CoinGecko (não é “preciso”, mas funciona como baseline).
    """
    q = market.question.lower()
    coin = None
    if "bitcoin" in q or re.search(r"\bbtc\b", q):
        coin = "bitcoin"
    elif "ethereum" in q or re.search(r"\beth\b", q):
        coin = "ethereum"
    elif "solana" in q or re.search(r"\bsol\b", q):
        coin = "solana"
    if not coin:
        return None

    cache_key = f"cg:{coin}"
    cached = crypto_cache.get(cache_key)
    if cached is not None:
        return cached

    url = "https://api.coingecko.com/api/v3/simple/price"
    data = await fetch_json(session, url, params={
        "ids": coin,
        "vs_currencies": "usd",
        "include_24hr_change": "true"
    })
    chg = safe_float(data.get(coin, {}).get("usd_24h_change"), 0.0)

    # Proxy: muda prob em função do momentum. (capado)
    fair = clamp01(0.5 + (chg / 100.0) * 0.8)  # 24h +10% => +0.08
    crypto_cache.set(cache_key, fair)
    return fair


async def fair_value_generic(session: aiohttp.ClientSession, market: Market) -> Optional[float]:
    """
    Para “outros mercados”: sem fonte externa, tenta detectar oportunidades
    só com microestrutura: gaps + liquidez/volume + prob extrema.
    Retorna um fair-value fraco (None) e deixa o score depender mais de microstructure.
    """
    return None


# =========================
# SCORING
# =========================
def microstructure_score(market: Market) -> float:
    """
    Score baseado só em liquidez/volume (para priorizar mercados bons).
    """
    # logs para não explodir
    v = math.log1p(max(0.0, market.volume_24h))
    l = math.log1p(max(0.0, market.liquidity))
    return 0.6 * v + 0.4 * l


def opportunity_score(pm_prob: float, fair_prob: Optional[float], market: Market) -> Tuple[float, float]:
    """
    Retorna (gap, score_total).
    Se não tiver fair_prob, score depende só de microestrutura + prob extrema.
    """
    ms = microstructure_score(market)

    if fair_prob is None:
        # Oportunidade “estrutura”: preço muito extremo + mercado forte
        extreme = abs(pm_prob - 0.5) * 2.0  # 0..1
        score = ms * (0.6 + 0.8 * extreme)
        gap = 0.0
        return gap, score

    gap = abs(pm_prob - fair_prob)
    # gap * microstructure, com peso extra para gap
    score = (gap * 5.0) * (0.4 + ms / 5.0)
    return gap, score


# =========================
# ANTI-SPAM
# =========================
class CooldownDB:
    """
    Guarda último alert por market_id (em arquivo json).
    """
    def __init__(self, path: str = "cooldown.json"):
        self.path = path
        self.data: Dict[str, int] = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except Exception:
            pass

    def can_alert(self, market_id: str) -> bool:
        last = self.data.get(market_id)
        if not last:
            return True
        return (now_ts() - last) >= COOLDOWN_SECONDS

    def mark_alerted(self, market_id: str):
        self.data[market_id] = now_ts()


# =========================
# PIPELINE
# =========================
async def analyze_market(session: aiohttp.ClientSession, market: Market) -> Optional[Dict[str, Any]]:
    # Se não tem prob do YES, não dá pra comparar
    if market.best_yes is None:
        return None

    q = market.question

    # Decide estimator
    fair = None
    if is_weather_market(q) or is_climate_market(q):
        fair = await fair_value_weather(session, market)
    elif is_crypto_market(q):
        fair = await fair_value_crypto(session, market)
    else:
        fair = await fair_value_generic(session, market)

    gap, score = opportunity_score(market.best_yes, fair, market)

    # Regras de corte
    # - Se tem fair: exige gap mínimo
    # - Sempre exige score mínimo
    if fair is not None and gap < MIN_GAP:
        return None
    if score < MIN_SCORE:
        return None

    return {
        "id": market.id,
        "question": market.question,
        "url": market.url,
        "category": market.category,
        "pm_prob_yes": market.best_yes,
        "fair_prob": fair,
        "gap": gap,
        "liquidity": market.liquidity,
        "volume_24h": market.volume_24h,
        "score": score
    }


async def scan_once(session: aiohttp.ClientSession, cooldown: CooldownDB):
    markets = await fetch_polymarket_markets(session)

    # Concurrency control
    sem = asyncio.Semaphore(MAX_CONCURRENCY)

    async def run_one(m: Market):
        async with sem:
            return await analyze_market(session, m)

    tasks = [run_one(m) for m in markets]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    opportunities: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        if r:
            opportunities.append(r)

    # Ordena por score desc
    opportunities.sort(key=lambda x: x["score"], reverse=True)

    # Envia só top N, respeitando cooldown
    sent = 0
    for op in opportunities[:10]:
        if not cooldown.can_alert(op["id"]):
            continue

        pm = op["pm_prob_yes"]
        fair = op["fair_prob"]
        gap = op["gap"]
        score = op["score"]

        if fair is None:
            txt = (
                f"📌 OPORTUNIDADE (microstructure)\n"
                f"• {op['question']}\n"
                f"• Prob(YES): {pm:.3f}\n"
                f"• Liquidez: {op['liquidity']:.0f} | Vol24h: {op['volume_24h']:.0f}\n"
                f"• Score: {score:.2f}\n"
                f"{op['url']}"
            )
        else:
            txt = (
                f"🚨 GAP DETECTADO\n"
                f"• {op['question']}\n"
                f"• Prob(YES): {pm:.3f}  vs  Fair: {fair:.3f}\n"
                f"• Gap: {gap:.3f} | Score: {score:.2f}\n"
                f"• Liquidez: {op['liquidity']:.0f} | Vol24h: {op['volume_24h']:.0f}\n"
                f"{op['url']}"
            )

        await telegram_send(session, txt)
        cooldown.mark_alerted(op["id"])
        sent += 1

    if sent:
        cooldown.save()


async def main():
    cooldown = CooldownDB()

    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        # IMPORTANTÍSSIMO: sem “status a cada 10s”.
        # Só roda scans no intervalo e manda msg apenas se achar oportunidade.
        while True:
            try:
                await scan_once(session, cooldown)
            except Exception as e:
                # sem spam. só printa no log
                print("scan error:", repr(e))

            await asyncio.sleep(SCAN_EVERY_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
