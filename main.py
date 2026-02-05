import os, time

token = (os.getenv("TELEGRAM_TOKEN") or "").strip()
print("TOKEN_EXISTS:", bool(token))
print("TOKEN_HAS_COLON:", ":" in token)
print("TOKEN_LEN:", len(token)) 

while True:
    time.sleep(60)
