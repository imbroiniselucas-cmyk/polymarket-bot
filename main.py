#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
POLYMARKET FAST SCANNER — 15 MIN MODE
- Nunca fica em silêncio
- Alertas claros: APOSTE A FAVOR / CONTRA
- Watchlist sempre que não houver edge
"""

import os, time, math, sqlite3, traceback, requests
from typing import Dict, Any, List

# ================= CONFIG =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "bot.db"

GAMMA_BASE = "https://gamma-api.polymarket.com"

# AGRESSIVIDADE
MIN_SCORE = 5.8
EDGE_MIN_PP = 2.2
CONF_MIN = 0.50

MIN_LIQ = 7000
MIN_VOL_DELTA = 60
MIN_PRICE_MOVE = 0.25

ALERT_COOLDOWN_MIN = 15
REALERT_EDGE_BUMP = 0.25
MAX_ALERTS = 10

FETCH_LIMIT = 1000
TIMEOUT = 10

# ================= UTILS =================
def ts(): 
    return int(time.time())

def clamp(x,a,b): 
    return max(a,min(b,x))

def f(x,d=0): 
    try: 
        return float(x)
    except: 
        return d

# ================= TELEGRAM =================
def send(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg,
                "disable_web_page_preview": True
            },
            timeout=TIMEOUT
        )
    except:
        pass

# ================= DB =================
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c=db();cur=c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS snap(
        t INT, id TEXT, yes REAL, vol REAL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS alert(
        t INT, id TEXT, edge REAL
    )""")
    c.commit();c.close()

def last_snap(mid):
    c=db();cur=c.cursor()
    cur.execute("SELECT * FROM snap WHERE id=? ORDER BY t DESC LIMIT 1",(mid,))
    r=cur.fetchone();c.close()
    return r

def save_snap(mid,yes,vol):
    c=db();cur=c.cursor()
    cur.execute("INSERT INTO snap VALUES(?,?,?,?)",(ts(),mid,yes,vol))
    c.commit();c.close()

def recent_alert(mid):
    c=db();cur=c.cursor()
    cur.execute(
        "SELECT * FROM alert WHERE id=? AND t>?",
        (mid,ts()-ALERT_COOLDOWN_MIN*60)
    )
    r=cur.fetchone();c.close()
    return r

def save_alert(mid,edge):
    c=db();cur=c.cursor()
    cur.execute("INSERT INTO alert VALUES(?,?,?)",(ts(),mid,edge))
    c.commit();c.close()

# ================= POLYMARKET =================
def fetch_markets():
    r=requests.get(
        f"{GAMMA_BASE}/markets",
        params={"limit":FETCH_LIMIT,"active":"true"},
        timeout=TIMEOUT
    )
    r.raise_for_status()
    return r.json()

# ================= LOGIC =================
def score(liq,vol_delta,pm):
    return (
        clamp(math.log10(liq+1)-2.7,0,4) +
        clamp(math.log10(max(vol_delta,0)+1)-1.8,0,4) +
        clamp(abs(pm)/1.2,0,3)
    )

def instruction(q,side):
    return f"🟢 APOSTE A FAVOR: {q}" if side=="YES" else f"🔴 APOSTE CONTRA: {q}"

# ================= MAIN =================
def main():
    init_db()
    send("✅ Scanner 15m rodou agora. Analisando mercados…")

    try:
        markets = fetch_markets()
    except Exception:
        send("❌ Erro ao buscar mercados do Polymarket.")
        return

    sent = 0
    watch = []

    for m in markets:
        mid = str(m.get("id"))
        q = m.get("question","(sem pergunta)")
        yes = f(m.get("outcomePrices",[0])[0])
        vol = f(m.get("volume",0))
        liq = f(m.get("liquidity",0))

        last = last_snap(mid)
        save_snap(mid,yes,vol)

        if not last:
            continue

        vol_delta = vol - f(last["vol"])
        pm = ((yes - f(last["yes"])) / max(f(last["yes"]),0.01)) * 100

        if liq < MIN_LIQ and vol < 50000:
            continue

        sc = score(liq,vol_delta,pm)
        if sc < MIN_SCORE:
            continue

        p_model = clamp(yes + pm/100*0.4,0.01,0.99)
        edge = (p_model - yes) * 100
        conf = clamp(sc/10,0,1)

        watch.append((q,yes,vol_delta,pm))

        if conf < CONF_MIN or abs(edge) < EDGE_MIN_PP:
            continue

        r = recent_alert(mid)
        if r and abs(edge) < abs(f(r["edge"])) + REALERT_EDGE_BUMP:
            continue

        side = "YES" if edge > 0 else "NO"

        send(
            "🚨 EDGE DETECTADO\n"
            f"{instruction(q,side)}\n"
            f"🎯 ENTRAR AGORA\n"
            f"🧠 Mercado ~{yes*100:.1f}% vs Modelo ~{p_model*100:.1f}%\n"
            f"📊 Edge {edge:+.2f}pp | ΔVol {vol_delta:.0f} | Move {pm:+.2f}%\n"
            f"https://polymarket.com/market/{m.get('slug','')}"
        )
        save_alert(mid,edge)
        sent += 1
        if sent >= MAX_ALERTS:
            break

    if sent == 0:
        if watch:
            msg = "⏳ MERCADOS EM MOVIMENTO (15min)\n"
            for w in watch[:5]:
                msg += f"- {w[0][:55]} | YES {w[1]:.3f} | ΔV {w[2]:.0f} | Δ% {w[3]:+.2f}\n"
            send(msg)
        else:
            send("👀 Primeira execução ou mercado parado. Próximo ciclo deve gerar sinais.")

# ================= RUN =================
if __name__ == "__main__":
    try:
        main()
    except Exception:
        send("❌ Bot crashou:\n"+traceback.format_exc()[-2000:])
