import os
import asyncio
import aiohttp
import telebot

# --- ENV (robusto) ---
BOT_TOKEN = (os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or "").strip()

# Debug seguro (não vaza token)
print("ENV BOT_TOKEN present?", bool(BOT_TOKEN))
print("ENV CHAT_ID present?", bool(CHAT_ID))
print(
    "ENV keys sample:",
    [k for k in ["BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "CHAT_ID", "TELEGRAM_CHAT_ID"] if k in os.environ],
)

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN não chegou nesse processo. "
        "Confere se a variável está no MESMO service/worker que roda /app/main.py "
        "e se o serviço foi reiniciado após setar as env vars."
    )

if not CHAT_ID:
    raise RuntimeError(
        "CHAT_ID não chegou nesse processo. "
        "Configure CHAT_ID (ou TELEGRAM_CHAT_ID) e reinicie o serviço."
    )

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


def enviar(msg: str):
    """Envia mensagem no Telegram (falha silenciosa se algo estiver errado)."""
    try:
        bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)
    except Exception as e:
        print("Falha ao enviar no Telegram:", repr(e))


async def fetch_json(url: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.json()


async def analisar_polymarket():
    """
    Versão simples:
    - puxa mercados
    - filtra por volume e liquidez
    - manda top oportunidades
    """
    try:
        url = "https://polymarket.com/api/markets"
        data = await fetch_json(url)

        markets = data.get("markets", [])
        if not isinstance(markets, list):
            enviar("⚠️ Resposta inesperada da API (markets não é lista).")
            return

        oportunidades = []
        for m in markets:
            volume = m.get("volume", 0) or 0
            liquidity = m.get("liquidity", 0) or 0
            slug = m.get("slug", "") or ""
            question = m.get("question", "Sem título") or "Sem título"

            # Ajuste os thresholds como quiser
            if volume > 200_000 and liquidity > 20_000 and slug:
                oportunidades.append((volume, liquidity, question, slug))

        # Ordena por volume desc
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
        print("ClientResponseError:", repr(e))
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
