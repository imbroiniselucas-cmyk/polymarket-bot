import os
import time
import requests
import telebot

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

bot = telebot.TeleBot(TOKEN)
GAMMA = "https://gamma-api.polymarket.com/markets"

# ====== AJUSTES SIMPLES (você pode mudar depois) ======
SCAN_SECONDS = 15 * 60          # 15 min
MIN_VOLUME = 20000              # mínimo volume (quanto maior, menos ruído)
MIN_LIQUIDITY = 8000            # mínimo liquidez
MIN_MOVE = 0.05                 # mínimo mudança de preço (0.05 = 5%)
COOLDOWN_MIN = 60               # não repetir alerta do mesmo mercado por 60 min
MAX_ALERTS_PER_SCAN = 5         # manda no máximo 5 alertas por rodada
# ======================================================

# memória em tempo real (enquanto o bot roda)
LAST_PRICE = {}      # market_id -> preço anterior
LAST_ALERT = {}      # market_id -> timestamp do último alerta

def enviar(msg):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

def buscar_mercados():
    r = requests.get(GAMMA, params={"limit": 250}, timeout=30)
    r.raise_for_status()
    return r.json()

def pegar_preco_sim(m):
    # Alguns mercados têm outcomePrices; se não tiver, ignoramos (pra manter simples)
    prices = m.get("outcomePrices")
    if not prices:
        return None
    try:
        return float(prices[0])  # geralmente o primeiro é "Yes"
    except:
        return None

def pode_alertar(market_id):
    agora = time.time()
    ultimo = LAST_ALERT.get(market_id, 0)
    return (agora - ultimo) >= (COOLDOWN_MIN * 60)

def marcar_alerta(market_id):
    LAST_ALERT[market_id] = time.time()

def formatar_alerta(m, preco_ant, preco_atual, move):
    titulo = m.get("question") or m.get("title") or "Mercado"
    slug = m.get("slug") or ""
    link = f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com"

    volume = int(float(m.get("volume", 0) or 0))
    liquidez = int(float(m.get("liquidity", 0) or 0))

    direcao = "⬆️" if preco_atual > preco_ant else "⬇️"
    pct = move * 100

    return (
        f"🚨 Alerta (qualificado)\n"
        f"{titulo}\n"
        f"{direcao} Preço: {preco_ant:.2f} → {preco_atual:.2f}  ({pct:.1f}%)\n"
        f"Volume: {volume} | Liquidez: {liquidez}\n"
        f"{link}"
    )

def rodar_scan():
    mercados = buscar_mercados()
    candidatos = []

    for m in mercados:
        if m.get("closed"):
            continue

        volume = float(m.get("volume", 0) or 0)
        liquidez = float(m.get("liquidity", 0) or 0)

        # 1) mercado grande
        if volume < MIN_VOLUME:
            continue
        if liquidez < MIN_LIQUIDITY:
            continue

        preco = pegar_preco_sim(m)
        if preco is None:
            continue

        market_id = str(m.get("id", "")) or None
        if not market_id:
            continue

        # primeira vez: só guarda preço
        if market_id not in LAST_PRICE:
            LAST_PRICE[market_id] = preco
            continue

        preco_ant = LAST_PRICE[market_id]
        move = abs(preco - preco_ant)

        # atualiza o preço guardado sempre
        LAST_PRICE[market_id] = preco

        # 2) movimento relevante
        if move < MIN_MOVE:
            continue

        # 3) anti-spam (cooldown)
        if not pode_alertar(market_id):
            continue

        # score simples: mais movimento + mais liquidez + mais volume = mais prioridade
        score = (move * 100) + (liquidez / 5000) + (volume / 50000)

        candidatos.append((score, m, preco_ant, preco, move))

    # manda só os melhores
    candidatos.sort(key=lambda x: x[0], reverse=True)
    melhores = candidatos[:MAX_ALERTS_PER_SCAN]

    if not melhores:
        enviar("🔎 Scan: nada forte o suficiente (bom sinal: menos ruído).")
        return

    enviar(f"🔔 Scan: {len(melhores)} alertas bons (filtrados).")
    for _, m, preco_ant, preco, move in melhores:
        market_id = str(m.get("id", ""))
        enviar(formatar_alerta(m, preco_ant, preco, move))
        marcar_alerta(market_id)

def main():
    enviar("🤖 Bot ligado: alertas melhores (volume + liquidez + movimento + anti-spam).")
    while True:
        try:
            rodar_scan()
        except Exception as e:
            enviar(f"⚠️ Erro: {e}")
        time.sleep(SCAN_SECONDS)

if __name__ == "__main__":
    main()
