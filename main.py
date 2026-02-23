#!/usr/bin/env python3
"""
main.py (no-requests version)
- Avoids 'requests' dependency completely (uses urllib).
- Sends alerts via Telegram if python-telegram-bot is installed.
- Provides Flask /health endpoint if flask is installed.
- Does NOT crash if telegram/flask are missing; it will fall back to console logs.

ENV VARS (optional):
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  POLL_INTERVAL_SECONDS=60
  LOG_LEVEL=INFO
  PORT=8080  (for Flask health, if Flask installed)

You can extend `fetch_polymarket_markets()` and `detect_opportunities()` with real logic.
"""

import os
import sys
import time
import json
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------
# Basic logging (no deps)
# ---------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def log(level: str, msg: str) -> None:
    if _LEVELS.get(level, 20) >= _LEVELS.get(LOG_LEVEL, 20):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{ts}] {level}: {msg}", flush=True)


# ---------------------------
# Safe imports (optional deps)
# ---------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

TelegramBot = None
telegram_available = False
try:
    from telegram import Bot as TelegramBot  # python-telegram-bot==13.15
    telegram_available = True
except Exception:
    telegram_available = False

flask_available = False
try:
    from flask import Flask
    flask_available = True
except Exception:
    flask_available = False


# ---------------------------
# HTTP helper (no requests)
# ---------------------------
def http_get_json(url: str, timeout: int = 20, headers: Optional[Dict[str, str]] = None) -> Any:
    """
    Fetch JSON from URL using urllib (built-in).
    """
    import urllib.request

    req = urllib.request.Request(url, method="GET")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8", errors="replace")
        return json.loads(data)


# ---------------------------
# Alerting
# ---------------------------
def send_alert(text: str) -> None:
    """
    Send alert to Telegram if possible, otherwise print to logs.
    """
    text = text.strip()
    if not text:
        return

    if telegram_available and BOT_TOKEN and CHAT_ID:
        try:
            bot = TelegramBot(token=BOT_TOKEN)
            bot.send_message(chat_id=CHAT_ID, text=text)
            log("INFO", f"Sent Telegram alert ({len(text)} chars).")
            return
        except Exception as e:
            log("ERROR", f"Telegram send failed: {e}")

    # Fallback: log to console
    log("INFO", f"ALERT:\n{text}")


# ---------------------------
# Polymarket scanning (stub)
# ---------------------------
def fetch_polymarket_markets() -> List[Dict[str, Any]]:
    """
    TODO: Replace with real Polymarket endpoint(s).
    Keep it conservative: this function must never crash the process.
    """
    # Example placeholder: empty list
    # If you have a URL, put it here, e.g.:
    # url = "https://<some-polymarket-api>/markets"
    # return http_get_json(url)
    return []


def market_is_allowed(m: Dict[str, Any]) -> bool:
    """
    Filter only climate + politics.
    This is a heuristic stub: adapt keys based on real API fields.
    """
    text = " ".join(
        str(m.get(k, "")) for k in ["category", "title", "slug", "description", "tags"]
    ).lower()

    climate_keywords = ["climate", "weather", "temperature", "rain", "snow", "wind", "hurricane", "storm"]
    politics_keywords = ["election", "president", "prime minister", "parliament", "vote", "poll", "senate", "politics"]

    return any(k in text for k in climate_keywords) or any(k in text for k in politics_keywords)


def detect_opportunities(markets: List[Dict[str, Any]]) -> List[str]:
    """
    TODO: Implement your real arbitrage/spread logic.
    For now, it demonstrates the pipeline safely.
    """
    alerts: List[str] = []
    for m in markets:
        if not market_is_allowed(m):
            continue

        # Placeholder example fields
        title = str(m.get("title", "Unknown market"))
        # Here you would compute:
        # - best bid/ask on YES/NO
        # - spread %
        # - exit potential
        # - timing/news signal
        #
        # Example dummy alert condition:
        if "weather" in title.lower():
            alerts.append(f"🛰️ Market (stub): {title}")

    return alerts


# ---------------------------
# Main loop (never die)
# ---------------------------
def run_scanner_forever() -> None:
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    last_alert_hashes = set()

    log("INFO", f"Scanner starting. poll={poll_interval}s telegram={telegram_available and bool(BOT_TOKEN and CHAT_ID)}")

    while True:
        try:
            markets = fetch_polymarket_markets()
            if not isinstance(markets, list):
                log("WARNING", "Markets response not a list; skipping.")
                markets = []

            opps = detect_opportunities(markets)

            for text in opps:
                h = hash(text)
                if h in last_alert_hashes:
                    continue
                last_alert_hashes.add(h)
                # keep memory bounded
                if len(last_alert_hashes) > 2000:
                    last_alert_hashes = set(list(last_alert_hashes)[-500:])
                send_alert(text)

        except Exception as e:
            log("ERROR", f"Scanner loop error: {e}")
            log("DEBUG", traceback.format_exc())

        time.sleep(max(5, poll_interval))


# ---------------------------
# Optional Flask health server
# ---------------------------
def run_health_server() -> None:
    if not flask_available:
        log("INFO", "Flask not available; skipping health server.")
        return

    app = Flask(__name__)

    @app.get("/health")
    def health():
        return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}

    port = int(os.getenv("PORT", "8080"))
    log("INFO", f"Starting Flask health server on 0.0.0.0:{port}")
    # Run in a thread so scanner can run too
    import threading

    t = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port), daemon=True)
    t.start()


def main() -> None:
    # Start health server (if Flask exists)
    run_health_server()

    # Start scanner (always)
    run_scanner_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("INFO", "Exiting on KeyboardInterrupt.")
        sys.exit(0)
    except Exception as e:
        log("ERROR", f"Fatal error: {e}")
        log("ERROR", traceback.format_exc())
        sys.exit(1)
