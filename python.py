#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""15m BTCUSDT SMC structure alerts using LuxAlgo's leg/pivot model.

Pivots use the supplied LuxAlgo defaults: internal=5, swing=50.
Per request, a break is confirmed by a wick touching/crossing a level.
"""
import json
import logging
import os
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("btc-smc")

SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
INTERNAL_LENGTH = int(os.getenv("LOOKBACK_INNER", "5"))
SWING_LENGTH = int(os.getenv("LOOKBACK_SWING", "50"))
KLINE_LIMIT = min(int(os.getenv("KLINE_LIMIT", "1000")), 1000)
STATE_FILE = "state.json"
EMAIL = os.getenv("QQ_EMAIL", "")
EMAIL_PASSWORD = os.getenv("QQ_EMAIL_PASSWORD", "")
EMAIL_TO = os.getenv("QQ_EMAIL_TO", EMAIL)
BINANCE = "https://api.binance.com"

def fetch_closed_klines():
    response = requests.get(
        f"{BINANCE}/api/v3/klines",
        params={"symbol": SYMBOL, "interval": INTERVAL, "limit": KLINE_LIMIT},
        timeout=20,
    )
    response.raise_for_status()
    now_ms = int(time.time() * 1000)
    candles = []
    for row in response.json():
        if int(row[6]) >= now_ms:
            continue
        candles.append({
            "time": int(row[0]), "close_time": int(row[6]),
            "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]),
        })
    return candles

def confirmed_legs(candles, length):
    """Return LuxAlgo-style leg changes, with pivot confirmation length bars later."""
    events = []
    leg = 0
    for confirm_idx in range(length, len(candles)):
        pivot_idx = confirm_idx - length
        preceding = candles[pivot_idx - length:pivot_idx]
        if len(preceding) < length:
            continue
        pivot = candles[pivot_idx]
        is_high = pivot["high"] > max(c["high"] for c in preceding)
        is_low = pivot["low"] < min(c["low"] for c in preceding)
        next_leg = 0
        if is_high:
            next_leg = -1
        elif is_low:
            next_leg = 1
        if next_leg and next_leg != leg:
            leg = next_leg
            side = "high" if leg == -1 else "low"
            events.append({
                "confirm_idx": confirm_idx, "pivot_idx": pivot_idx,
                "side": side, "price": pivot[side], "pivot_time": pivot["time"],
            })
    return events

def reconstruct(candles, length):
    pivots = confirmed_legs(candles, length)
    high = low = None
    high_pivot_time = low_pivot_time = None
    trend = 0
    pivot_by_confirmation = {}
    for pivot in pivots:
        pivot_by_confirmation.setdefault(pivot["confirm_idx"], []).append(pivot)
    for i, candle in enumerate(candles):
        for pivot in pivot_by_confirmation.get(i, []):
            if pivot["side"] == "high":
                high, high_pivot_time = pivot["price"], pivot["pivot_time"]
            else:
                low, low_pivot_time = pivot["price"], pivot["pivot_time"]
        if trend == 0 and pivot_by_confirmation.get(i):
            trend = 1 if pivot_by_confirmation[i][-1]["side"] == "low" else -1

    # Only evaluate the newest closed candle for a new wick cross; never replay historical alerts.
    if len(candles) < 2:
        return {"trend": trend, "high": high, "low": low,
                "high_pivot_time": high_pivot_time, "low_pivot_time": low_pivot_time}, []
    prev, cur = candles[-2], candles[-1]
    alerts = []
    if high is not None and prev["high"] < high <= cur["high"]:
        kind = "BULL_BOS" if trend == 1 else "BULL_CHOCH"
        alerts.append({"kind": kind, "level": high, "wick": cur["high"],
                       "candle_close": cur["close"], "time": cur["close_time"],
                       "pivot_time": high_pivot_time})
        trend = 1
    if low is not None and prev["low"] > low >= cur["low"]:
        kind = "BEAR_BOS" if trend == -1 else "BEAR_CHOCH"
        alerts.append({"kind": kind, "level": low, "wick": cur["low"],
                       "candle_close": cur["close"], "time": cur["close_time"],
                       "pivot_time": low_pivot_time})
        trend = -1
    return {"trend": trend, "high": high, "low": low,
            "high_pivot_time": high_pivot_time, "low_pivot_time": low_pivot_time}, alerts

def read_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            value = json.load(handle)
            return value if value.get("version") == 3 else {"version": 3, "last_processed": 0}
    except (OSError, ValueError):
        return {"version": 3, "last_processed": 0}

def write_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)
    os.replace(temporary, STATE_FILE)

def send_email(subject, body):
    if not (EMAIL and EMAIL_PASSWORD and EMAIL_TO):
        raise RuntimeError("Configure QQ_EMAIL, QQ_EMAIL_PASSWORD and QQ_EMAIL_TO as GitHub Secrets")
    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = Header(subject, "utf-8")
    message["From"], message["To"] = EMAIL, EMAIL_TO
    with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as smtp:
        smtp.login(EMAIL, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL, [EMAIL_TO], message.as_string())

def main():
    candles = fetch_closed_klines()
    if len(candles) < 2 * SWING_LENGTH + 2:
        raise RuntimeError(f"Not enough closed candles: {len(candles)}")
    previous_state = read_state()
    current_close_time = candles[-1]["close_time"]
    if current_close_time <= previous_state.get("last_processed", 0):
        log.info("Already processed closed candle %s", current_close_time)
        return

    all_alerts = []
    summaries = []
    for name, length in (("Internal", INTERNAL_LENGTH), ("Swing", SWING_LENGTH)):
        structure, alerts = reconstruct(candles, length)
        summaries.append(f"{name}: trend={structure['trend']}, active_high={structure['high']}, active_low={structure['low']}")
        for alert in alerts:
            all_alerts.append((name, alert))
    log.info("Structure levels: %s", " | ".join(summaries))

    if all_alerts:
        current = candles[-1]["close"]
        lines = [f"BTCUSDT {INTERVAL} SMC wick-touch alert",
                 f"Signal candle close: {current:,.2f} USDT",
                 f"Signal candle high/low: {candles[-1]['high']:,.2f} / {candles[-1]['low']:,.2f} USDT"]
        for name, alert in all_alerts:
            lines.append(f"{name} {alert['kind']}: structure level {alert['level']:,.2f}; touched wick {alert['wick']:,.2f}; candle close {alert['candle_close']:,.2f}")
        send_email("BTC 15m SMC signal", "\n".join(lines))
    else:
        log.info("No new structure wick break on the latest closed candle")

    previous_state.update({
        "version": 3, "last_processed": current_close_time,
        "internal": reconstruct(candles, INTERNAL_LENGTH)[0],
        "swing": reconstruct(candles, SWING_LENGTH)[0],
    })
    write_state(previous_state)

if __name__ == "__main__":
    main()
