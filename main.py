import os
import telebot

TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("CHAT_ID") or "").strip()

print("TOKEN set?", bool(TOKEN))
print("CHAT_ID:", CHAT_ID)

bot = telebot.TeleBot(TOKEN)

bot.send_message(CHAT_ID, "✅ TESTE: se chegou isso, Telegram está OK.", disable_web_page_preview=True)
print("sent!")
