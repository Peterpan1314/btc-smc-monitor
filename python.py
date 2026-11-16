#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BTCUSDT 15m LuxAlgo-style SMC structure alerts.

Swing/internal pivots follow LuxAlgo's leg transition model (default lengths 50/5).
Breaks are intentionally wick-triggered per operator preference.
"""
import json, logging, os, smtplib, time
from email.header import Header
from email.mime.text import MIMEText
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("btc-smc")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
PIVOT_INTERNAL = int(os.getenv("LOOKBACK_INNER", "5"))
PIVOT_SWING = int(os.getenv("LOOKBACK_SWING", "50"))
LIMIT = int(os.getenv("KLINE_LIMIT", "1000"))
COOLDOWN = int(os.getenv("COOLDOWN_SEC", "0"))
EMAIL = os.getenv("QQ_EMAIL", "")
PASSWORD = os.getenv("QQ_EMAIL_PASSWORD", "")
TO = os.getenv("QQ_EMAIL_TO", EMAIL)
STATE_PATH = "state.json"
BASE = "https://api.binance.com"

def fetch_klines(symbol=SYMBOL, interval=INTERVAL, limit=LIMIT):
    r = requests.get(BASE + "/api/v3/klines", params={"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}, timeout=20)
    r.raise_for_status()
    now = int(time.time() * 1000)
    # Exclude currently forming bar; the monitor runs every 15m and confirms the last closed candle.
    return [{"open_time": int(x[0]), "open": float(x[1]), "high": float(x[2]),
             "low": float(x[3]), "close": float(x[4]), "close_time": int(x[6])}
            for x in r.json() if int(x[6]) < now]

def empty_state():
    return {"version": 2, "trend": 0, "high": None, "low": None,
            "high_time": None, "low_time": None, "last_break_high": None,
            "last_break_low": None, "last_alert_time": 0}

def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f: return json.load(f)
    except (OSError, ValueError): return {}

def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def find_leg_pivots(candles, length):
    """LuxAlgo leg() semantics: compare bar[length] with the preceding length bars.
    Pivot is confirmed length bars after its origin; later pivots replace same-side levels.
    """
    pivots = []
    for i in range(length, len(candles)):
        prior = candles[i-length:i]
        pivot = candles[i-length]
        new_high = pivot["high"] > max(x["high"] for x in prior)
        new_low = pivot["low"] < min(x["low"] for x in prior)
        if new_high: pivots.append((i, "high", pivot["high"], pivot["open_time"]))
        elif new_low: pivots.append((i, "low", pivot["low"], pivot["open_time"]))
    return pivots

def detect_structure(candles, length, previous):
    state = dict(previous or {})
    if state.get("version") != 2 or state.get("length") != length:
        state = empty_state()
        # Replay closed history to reconstruct trend and live levels, without emitting old alerts.
        replay = True
    else: replay = False
    state["length"] = length
    # Process pivots incrementally in chronological order and test wick breaks after pivot updates.
    # Rebuild from a stable history window on every scheduled run, deduping by pivot timestamps.
    state = empty_state() | {"length": length}
    events = []
    pivots = find_leg_pivots(candles, length)
    by_confirm = {}
    for p in pivots: by_confirm.setdefault(p[0], []).append(p)
    trend = 0
    last_high = last_low = None
    high_time = low_time = None
    for i, bar in enumerate(candles):
        for _, side, price, origin_time in by_confirm.get(i, []):
            if side == "high": last_high, high_time = price, origin_time
            else: last_low, low_time = price, origin_time
        if trend == 0:
            ps = by_confirm.get(i, [])
            if ps: trend = -1 if ps[-1][1] == "high" else 1
        # A touch is a crossing from the prior bar's range into the level.
        if i == 0: continue
        prev = candles[i-1]
        if last_high is not None and prev["high"] < last_high <= bar["high"]:
            kind = "BULL_BOS" if trend == 1 else "BULL_CHOCH"
            events.append({"kind":kind, "level":last_high,"price":last_high,"time":bar["close_time"],"pivot_time":high_time})
            trend = 1
            last_high = None
        elif last_low is not None and prev["low"] > last_low >= bar["low"]:
            kind = "BEAR_BOS" if trend == -1 else "BEAR_CHOCH"
            events.append({"kind":kind, "level":last_low,"price":last_low,"time":bar["close_time"],"pivot_time":low_time})
            trend = -1
            last_low = None
    state.update({"trend":trend,"high":last_high,"low":last_low,"high_time":high_time,"low_time":low_time,
                  "last_break_high":next((e["time"] for e in reversed(events) if e["kind"].startswith("BULL")),None),
                  "last_break_low":next((e["time"] for e in reversed(events) if e["kind"].startswith("BEAR")),None)})
    # Only the newest candle's events can be new notifications; older events are historical reconstruction.
    latest = candles[-1]["close_time"] if candles else None
    return state, [e for e in events if e["time"] == latest]

def send_email(subject, body):
    if not (EMAIL and PASSWORD and TO):
        raise RuntimeError("QQ_EMAIL, QQ_EMAIL_PASSWORD and QQ_EMAIL_TO secrets must be configured")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8"); msg["From"] = EMAIL; msg["To"] = TO
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as smtp:
        smtp.login(EMAIL, PASSWORD); smtp.sendmail(EMAIL, [TO], msg.as_string())

def main():
    candles = fetch_klines()
    if len(candles) < PIVOT_SWING * 2 + 3: raise RuntimeError("Insufficient closed candles")
    old = load_state()
    # Backfill only to reconstruct current structure, then alert on newest closed candle.
    results = []
    new = {"version":2}
    for name, length in (("internal",PIVOT_INTERNAL),("swing",PIVOT_SWING)):
        state, events = detect_structure(candles, length, old.get(name))
        new[name] = state
        results.extend((name, e) for e in events)
    if results:
        lines = [f"BTCUSDT 15m SMC wick-touch alert (closed candle)"]
        for name,e in results:
            lines.append(f'{name}: {e["kind"]}; broken level/trigger price {e["price"]:,.2f}; candle close {candles[-1]["close"]:,.2f}')
        send_email("BTC 15m SMC signal", "\n".join(lines))
    new["last_run"] = int(time.time())
    save_state(new)

if __name__ == "__main__":
    main()
