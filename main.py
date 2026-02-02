import os
import time
import requests
import telebot
from datetime import datetime

# =========================
# CONFIG / ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise RuntimeError("Faltam TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID no ambiente.")

bot = telebot.TeleBot(TOKEN)
GAMMA = "https://gamma-api.polymarket.com/markets"

# =========================
# AJUSTES (SEMANA 1)
# =========================
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "60"))  # 30-60 recomendado

# Filtros suaves (pra gerar sinal e depois apertar)
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "5000"))        # volume total do mercado
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "1000"))  # liquidez total

# Gatilhos (delta)
PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "0.02"))   # 0.02 = 2% em 1 scan
VOLUME_JUMP = float(os.environ.get("VOLUME_JUMP", "2000"))         # +$2000 desde o último scan

# Anti-spam: cooldown por (market_id, tipo)
COOLDOWN_PRICE_MIN = int(os.environ.get("COOLDOWN_PRICE_MIN", "5"))
COOLDOWN_VOLUME_MIN = int(os.environ.get("COOLDOWN_VOLUME_MIN", "5"))

# Limites
MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "8"))

# Healthcheck
HEALTH_EVERY_MIN = int(os.environ.get("HEALTH_EVERY_MIN", "15"))

# Debug
DEBUG = os.environ.get("DEBUG", "1") == "1"  # imprime no console
SEND_DEBUG_TO_TELEGRAM = os.environ.get("SEND_DEBUG_TO_TELEGRAM", "0") == "1"

# =========================
# STATE (memória)
# =========================
STATE = {
    "last": {},       # market_id -> {"price": float, "volume": float, "ts": float}
    "cooldown": {},   # (market_id, alert_type) -> last_sent_ts
    "stats": {
        "start_ts": time.time(),
        "scans": 0,
        "alerts": 0,
        "last_health_ts": 0,
    }
}

# =========================
# HELPERS
# =========================
def now_ts() -> float:
    return time.time()

def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")

def enviar(msg: str):
    bot.send_message(CHAT_ID, msg, disable_web_page_preview=True)

def log(msg: str):
    if DEBUG:
        print(msg, flush=True)
    if SEND_DEBUG_TO_TELEGRAM:
        #
