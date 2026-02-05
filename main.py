#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, time

def env(name: str) -> str:
    return (os.getenv(name) or "").strip()

def mask(s: str) -> str:
    if not s:
        return ""
    return s[:4] + "..." + s[-4:] if len(s) > 8 else s[:2] + "..." + s[-2:]

def write_file(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def main():
    # file proves the container actually ran your code
    write_file("BOOT_OK.txt", "BOOT OK\n")

    token = env("TELEGRAM_TOKEN")
    chat_id = env("CHAT_ID")

    status = []
    status.append("ENV_STATUS")
    status.append(f"TELEGRAM_TOKEN exists: {bool(token)}")
    status.append(f"TELEGRAM_TOKEN has_colon: {':' in token}")
    status.append(f"TELEGRAM_TOKEN length: {len(token)}")
    status.append(f"TELEGRAM_TOKEN masked: {mask(token)}")
    status.append(f"CHAT_ID exists: {bool(chat_id)}")
    status.append(f"CHAT_ID value: {chat_id}")
    status.append("ENV_KEYS_SAMPLE: " + ", ".join(sorted(list(os.environ.keys()))[:40]))
    status_txt = "\n".join(status) + "\n"

    write_file("ENV_STATUS.txt", status_txt)

    # keep alive so files persist while container runs
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
