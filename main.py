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
# AJUSTES (AGRESSIVO)
# =========================
SCAN_SECONDS = int(os.environ.get("SCAN_SECONDS", "30"))  # mais rápido

# Filtros bem suaves (pra gerar sinal)
MIN_VOLUME = float(os.environ.get("MIN_VOLUME", "1000"))
MIN_LIQUIDITY = float(os.environ.get("MIN_LIQUIDITY", "200"))

# Gatilhos (delta)
PRICE_MOVE_PCT = float(os.environ.get("PRICE_MOVE_PCT", "0.01"))   # 1% por scan
VOLUME_JUMP = float(os.environ.get("VOLUME_JUMP", "500"))          # +500 por scan

# Anti-spam por (market_id, tipo)
COOLDOWN_PRICE_MIN = int(os.environ.get("COOLDOWN_PRICE_MIN", "2"))
COOLDOWN_VOLUME_MIN = int(os.environ.get("COOLDOWN_VOLUME_MIN", "2"))

# Limites
MAX_ALERTS_PER_SCAN = int(os.environ.get("MAX_ALERTS_PER_SCAN", "10"))

# Healthcheck
HEALTH_EVERY_MIN = int(os.environ.get("HEALTH_EVERY_MIN", "10"))

# Debug
DEBUG = os.environ.get("DEBUG", "1") == "1"  # imprime no console/logs
SEND_DEBUG_TO_TELEGRAM = os.environ.get("SEND_DEBUG_TO_TELEGRAM", "0") == "1"

# =========================
# STATE (memória)
# =========================
STATE


