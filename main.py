import os, time

def show(k):
    v = os.getenv(k)
    if v is None:
        print(k, "=", "None")
    else:
        print(k, "LEN=", len(v), "START=", repr(v[:6]))

show("TELEGRAM_TOKEN")
show("TELEGRAM_CHAT_ID")

# fica vivo pra você ver o log
while True:
    time.sleep(60)
