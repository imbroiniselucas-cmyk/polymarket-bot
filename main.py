import os
import asyncio
import aiohttp
import telebot

# --- ENV (robusto) ---
BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()

print("ENV BOT_TOKEN present?", bool(BOT_TOKEN))
print("ENV CHAT_ID present?", bool(CHAT_ID))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não chegou nesse processo.")
if not CHAT_ID:
    raise RuntimeError("CHAT_ID não chegou nesse processo.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def enviar(msg: str):
    try:
        bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)
    except Exception as e:
        print("Falha ao enviar no Telegram:", repr(e))


async def fetch_json(url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
            resp.raise_for_status()
            return await resp.json()


async def analisar_polymarket():
    try:
        # ✅ Gamma API (mercados)
        url = "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=200"
        data = await fetch_json(url)

        # Gamma costuma retornar LISTA
        markets = data if isinstance(data, list) else data.get("markets", [])

        oportunidades = []
        for m in markets:
            volume = m.get("volume", 0) or 0
            liquidity = m.get("liquidity", 0) or 0
            slug = m.get("slug", "") or ""
            question = m.get("question", "Sem título") or "Sem título"

            if volume > 200_000 and liquidity > 20_000 and slug:
                oportunidades.append((volume, liquidity, question, slug))

        oportunidades.sort(key=lambda x: x[0], reverse=True)

        if oportunidades:
            linhas = []
            for volume, liquidity, question, slug in oportunidades[:10]:
                linhas.append(
                    f"📊 <b>{question}</b>\n"
                    f"Vol: {int(volume)} | Liq: {int(liquidity)}\n"
                    f"https://polymarket.com/market/{slug}"
                )
            enviar("🚨 <b>OPORTUNIDADES DETECTADAS</b>\n\n" + "\n\n".join(linhas))
        else:
            enviar("🤖 Sem oportunidades relevantes no momento.")

    except aiohttp.ClientResponseError as e:
        enviar(f"❌ API erro: <code>{e.status}</code>")
        print("ClientResponseError:", e.status, e.message)
    except Exception as e:
        enviar(f"❌ Erro na análise:\n<code>{e}</code>")
        print("Erro geral:", repr(e))


async def main():
    enviar("🤖 Bot ligado. Vou checar oportunidades a cada 15 min.")
    while True:
        await analisar_polymarket()
        await asyncio.sleep(900)  # 15 minutos


if __name__ == "__main__":
    asyncio.run(main())
