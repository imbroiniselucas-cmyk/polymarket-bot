import os
import time
import requests
import telebot

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

bot = telebot.TeleBot(TOKEN)

def enviar(msg):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

def buscar_mercados():
    r = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"limit": 200},
        timeout=30
    )
    return r.json()

def filtrar(mercados):
    bons = []
    for m in mercados:
        if m.get("closed"):
            continue

        volume = m.get("volume", 0)
        liquidez = m.get("liquidity", 0)

        if volume and liquidez and volume > 10000 and liquidez > 5000:
            bons.append(m)

    return bons[:5]

enviar("🤖 Bot ligado. Vou mandar sugestões do Polymarket.")

while True:
    try:
        mercados = buscar_mercados()
        bons = filtrar(mercados)

        if not bons:
            enviar("🔎 Nenhuma oportunidade agora.")
        else:
            enviar("📊 Sugestões do Polymarket:")
            for m in bons:
                titulo = m.get("question", "Mercado")
                slug = m.get("slug", "")
                link = f"https://polymarket.com/market/{slug}"
                enviar(f"• {titulo}\n{link}")

    except Exception as e:
        enviar(f"Erro: {e}")

    time.sleep(900)
