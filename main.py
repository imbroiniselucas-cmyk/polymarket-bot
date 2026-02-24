import os
import json
import urllib.request
import urllib.parse
import time
import logging

# =========================
# ENV VARIABLES
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set!")
    exit(1)

print("✅ Environment variables loaded")

# Set up logging
logging.basicConfig(level=logging.INFO)

# =========================
# TELEGRAM SEND FUNCTION
# =========================
def send_telegram_message(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": text
        }).encode()

        req = urllib.request.Request(url, data=data)
        response = urllib.request.urlopen(req)
        result = response.read().decode()

        logging.info("📩 Telegram response: %s", result)

    except Exception as e:
        logging.error("❌ Telegram send error: %s", e)

# =========================
# MAIN
# =========================
def main():
    send_telegram_message("🚀 Bot deployed successfully and connected to Telegram.")

    # Keep the bot running
    try:
        while True:
            time.sleep(10)  # Sleep to prevent high CPU usage
    except KeyboardInterrupt:
        logging.info("Bot terminated.")

if __name__ == "__main__":
    main()
