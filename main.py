import os
import telebot

TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()

if not TOKEN or ":" not in TOKEN:
    # Mantém o processo vivo pra você ver que está rodando (mesmo sem log)
    # Se isso acontecer, é 100% variável não chegando no runtime.
    while True:
        pass

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=["start"])
def start(m):
    bot.reply_to(m, "✅ Online. Eu estou rodando e recebi seu /start.")

bot.infinity_polling()
