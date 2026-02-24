import os
import urllib.request
import urllib.parse
import json
import time
import logging

logging.basicConfig(level=logging.INFO)

# =========================
# LOAD ENV VARIABLES
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print("🔎 Checking environment variables...")

if not BOT_TOKEN:
    print("❌ TELEGRAM_BOT_TOKEN not found!")
else:
    print("✅ TELEGRAM_BOT_TOKEN loaded")

if not CHAT_ID:
    print("❌ TELEGRAM_CHAT_ID not found!")
else:
    print("✅ TELEGRAM_CHAT_ID loaded")

if not BOT_TOKEN or not CHAT_ID:
    raise SystemExit("⛔ Missing required environment variables.")

# =========================
# TELEGRAM FUNCTION
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

        print("📩 Telegram raw response:")
        print(result)

        parsed = json.loads(result)

        if not parsed.get("ok"):
            print("❌ Telegram API returned error!")
        else:
            print("✅ Message sent successfully!")

    except Exception as e:
        print("❌ Exception sending message:", e)

# =========================
# MAIN
# =========================
def main():
    print("🚀 Bot starting...")
    send_telegram_message("🚀 Railway deployment successful. Bot connected.")

    while True:
        time.sleep(15)

if __name__ == "__main__":
    main()
