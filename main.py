import os
import time
import requests
import telebot

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

bot = telebot.TeleBot(TOKEN)
GAMMA = "https://gamma-api.polymarket.com/markets"

# memória simples (guarda último preço)
ULTIMO_PRECO = {}

def enviar(msg):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

def buscar_mercados():
    r = requests.get(GAMMA, params={"limit": 200}, timeout=30)
    return r.json()

def analisar():
    mercados = buscar_mercados()

    for m in mercados:
        if m.get("closed"):
            continue

        prices = m.get("outcomePrices")
        if not prices:
            continue

        preco = float(prices[0])  # SIM
        market_id = m.get("id")

        # primeira vez: só salva
        if market_id not in ULTIMO_PRECO:
            ULTIMO_PRECO[market_id] = preco
            continue

        preco_antigo = ULTIMO_PRECO[market_id]
        variacao = abs(preco - preco_antigo)

        # só alerta se mudou "bastante"
        if variacao >= 0.05:
            titulo = m.get("question", "Mercado")
            slug = m.get("slug", "")
            link = f"https://polymarket.com/market/{slug}"

            enviar(
                f"🚨 Movimento detectado\n"
                f"{titulo}\n"
                f"Preço mudou de {preco_antigo:.2f} → {preco:.2f}\n"
                f"{link}"
            )

            ULTIMO_PRECO[market_id] = preco

def main():
    enviar("🤖 Bot ligado (modo: alerta por movimento de preço).")
    while True:
        try:
            analisar()
        except Exception as e:
            enviar(f"Erro: {e}")

        time.sleep(900)  # 15 min

if __name__ == "__main__":
    main()
