import os
import json
import urllib.request
import urllib.parse

# =========================
# ENV VARIABLES
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not BOT_TOKEN or not CHAT_ID:
    print("❌ TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set!")
    exit(1)

print("✅ Environment variables loaded")


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

        print("📩 Telegram response:", result)

    except Exception as e:
        print("❌ Telegram send error:", e)


# =========================
# MAIN
# =========================
def main():
    send_telegram_message("🚀 Bot deployed successfully and connected to Telegram.")

    # Bot fica rodando sem enviar mensagens automáticas
    while True:
        pass


if __name__ == "__main__":
    main()
