import os
import time
import telebot

TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()

print("TOKEN_LEN:", len(TOKEN))
print("CHAT_ID:", repr(CHAT_ID))

if not TOKEN or not CHAT_ID:
    print("MISSING_VARS")
    while True:
        time.sleep(60)

bot = telebot.TeleBot(TOKEN)

try:
    bot.send_message(CHAT_ID, "✅ TESTE: Railway -> Telegram OK (mensagem de teste).")
    print("SENT_OK")
except Exception as e:
    print("SEND_FAILED:", repr(e))

time.sleep(60)
