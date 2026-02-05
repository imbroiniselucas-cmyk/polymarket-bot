import os
import telebot

TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
if not TOKEN or ":" not in TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN missing/invalid in runtime")

bot = telebot.TeleBot(TOKEN)

@bot.message_handler(commands=["start"])
def start(m):
    bot.reply_to(m, f"✅ Online.\nChat ID: {m.chat.id}\nNow send /ping")

@bot.message_handler(commands=["ping"])
def ping(m):
    bot.reply_to(m, "pong ✅")

bot.infinity_polling(timeout=30, long_polling_timeout=30)
