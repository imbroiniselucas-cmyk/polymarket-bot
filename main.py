#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time

def env(name: str) -> str:
    return (os.getenv(name) or "").strip()

def mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 10:
        return s[0:2] + "..." + s[-2:]
    return s[0:4] + "..." + s[-4:]

def log_env():
    token = env("TELEGRAM_TOKEN")
    chat_id = env("CHAT_ID")

    print("=== ENV DIAG ===")
    print("TELEGRAM_TOKEN exists:", bool(token))
    print("TELEGRAM_TOKEN has_colon:", (":" in token))
    print("TELEGRAM_TOKEN length:", len(token))
    print("TELEGRAM_TOKEN masked:", mask(token))
    print("CHAT_ID exists:", bool(chat_id))
    print("CHAT_ID value:", chat_id)
    print("ALL ENV KEYS SAMPLE (first 30):", sorted(list(os.environ.keys()))[:30])
    print("================")

def try_send_boot():
    token = env("TELEGRAM_TOKEN")
    chat_id = env("CHAT_ID")

    if (not token) or (":" not in token):
        return False, "Token missing/invalid at runtime"
    if not chat_id:
        return False, "CHAT_ID missing at runtime"

    try:
        import telebot
        bot = telebot.TeleBot(token)
        bot.send_message(chat_id, "✅ BOOT OK — Telegram env vars are visible to Railway runtime.")
        return True, "Sent OK"
    except Exception as e:
        return False, f"Telegram send failed: {type(e).__name__}: {str(e)[:160]}"

def main():
    sent_once = False
    while True:
        log_env()
        ok, msg = try_send_boot()
        print("[BOOT TEST]", ok, msg)

        # Don't spam Telegram; only try to send once per successful boot
        if ok:
            sent_once = True
            # Keep running (so you can swap back to the real bot after)
            time.sleep(300)
        else:
            time.sleep(30)

if __name__ == "__main__":
    main()
