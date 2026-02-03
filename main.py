import os
import asyncio
import aiohttp
import telebot
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def enviar(msg):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

async def fetch_json(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as resp:
            return await resp.json()

async def analisar_polymarket():
    try:
        url = "https://polymarket.com/api/markets"
        data = await fetch_json(url)

        oportunidades = []

        for m in data.get("markets", []):
            volume = m.get("volume", 0)
            liquidity = m.get("liquidity", 0)

            if volume > 200_000 and liquidity > 20_000:
                oportunidades.append(
                    f"📊 <b>{m.get('question')}</b>\n"
                    f"Vol: {volume} | Liq: {liquidity}\n"
                    f"https://polymarket.com/market/{m.get('slug')}"
                )

        if oportunidades:
            enviar("🚨 <b>OPORTUNIDADES DETECTADAS</b>\n\n" + "\n\n".join(oportunidades))
        else:
            enviar("🤖 Sem oportunidades relevantes no momento.")

    except Exception as e:
        enviar(f"❌ Erro na análise:\n<code>{e}</code>")

async def main():
    enviar("🤖 Bot ligado e analisando Polymarket.")
    while True:
        await analisar_polymarket()
        await asyncio.sleep(900)  # 15 minutos

if __name__ == "__main__":
    asyncio.run(main())
