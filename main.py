import os
import time
import requests
import telebot
from datetime import datetime

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

if not TOKEN or not CHAT_ID:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

bot = telebot.TeleBot(TOKEN)
GAMMA_URL = "https://gamma-api.polymarket.com/markets"

# =========================
# SETTINGS (AGGRESSIVE)
# =========================
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "30"))

MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "1000"))
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "200"))

PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "0.01"))  # 1%
VOLUME_JUMP = float(os.environ.get("VOLUME_JUMP", "500"))         # +500

COOLDOWN_PRICE_MIN = int(os.environ.get("COOLDOWN_PRICE_MIN", "2"))
COOLDOWN_VOLUME_MIN = int(os.environ.get("COOLDOWN_VOLUME_MIN", "2"))

MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "10"))

HEALTH_EVERY_MIN = int(os.environ.get("HEALTH_EVERY_MIN", "10"))

DEBUG = os.environ.get("DEBUG", "1") == "1"
SEND_DEBUG_TO_TELEGRAM = os.environ.get("SEND_DEBUG_TO_TELEGRAM", "0") == "1"

# =========================
# STATE
# =========================
last_state = {}     # market_id -> {"price": float, "volume": float, "ts": float}
cooldowns = {}      # (market_id, type) -> last_sent_ts

start_ts = time.time()
scan_count = 0
alert_count = 0
last_health_ts = 0

# =========================
# HELPER
