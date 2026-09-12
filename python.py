#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 15m SMC Internal Structure Monitor (BOS/CHoCH)
- 币安公开 API（无需 Key）
- 状态存 GitHub Gist（跨运行持久化）
- 同时检测内部结构 + Swing 结构的 CHoCH/BOS
- 防重复报警：同一结构位小幅波动不重复发，加冷却时间
- 信号触发 → Telegram Bot / QQ邮箱 / Server酏（微信）
- 由 GitHub Actions 每 15 分钟运行一次
"""

import os
import json
import time
import logging
import requests
import smtplib
from email.mime.text import MIMEText
from email.header import Header

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("smc")


# ============================================================
# 配置（从 GitHub Secrets 读）
# ============================================================
SYMBOL         = os.environ.get("SYMBOL", "BTCUSDT")
INTERVAL       = os.environ.get("INTERVAL", "15m")
KLINE_LIMIT    = int(os.environ.get("KLINE_LIMIT", "300"))

# 两个结构的 lookback
LOOKBACK_INNER = int(os.environ.get("LOOKBACK_INNER", "5"))   # 内部结构
LOOKBACK_SWING = int(os.environ.get("LOOKBACK_SWING", "20"))  # Swing 结构

# 通知渠道
TG_TOKEN       = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID     = os.environ.get("TG_CHAT_ID", "")
USE_TG         = bool(TG_TOKEN and TG_CHAT_ID)

QQ_EMAIL       = os.environ.get("QQ_EMAIL", "")
QQ_SMTP_PW     = os.environ.get("QQ_EMAIL_PASSWORD", "")
QQ_EMAIL_TO    = os.environ.get("QQ_EMAIL_TO", QQ_EMAIL)
USE_QQ         = bool(QQ_EMAIL and QQ_SMTP_PW)

SC_SENDKEY     = os.environ.get("SC_SENDKEY", "")
USE_SC         = bool(SC_SENDKEY)

# Gist 状态持久化
GIST_ID        = os.environ.get("GIST_ID", "")
GH_TOKEN       = os.environ.get("GH_TOKEN", "")
USE_GIST       = bool(GIST_ID and GH_TOKEN)

GIST_API       = "https://api.github.com/gists"
HEADERS        = {"Authorization": f"token {GH_TOKEN}",
                  "Accept": "application/vnd.github.v3+json"} if GH_TOKEN else {}

# 防重复报警参数
MIN_PCT_CHANGE = float(os.environ.get("MIN_PCT_CHANGE", "0.3"))  # 至少偏移百分比
COOLDOWN_SEC   = int(os.environ.get("COOLDOWN_SEC", "1800"))     # 冷却秒数（默认 30 分钟）


# ============================================================
# 币安公开 K线
# ============================================================
def fetch_klines(symbol, interval, limit):
    """
    从 Yahoo Finance 获取 BTC-USD 15m K 线数据
    Yahoo 对 GitHub Actions 友好，无需 API key
    """
    import yfinance as yf
    ticker = "BTC-USD"
    try:
        df = yf.download(ticker, period="7d", interval="15m", progress=False, timeout=30)
        if df.empty:
            log.error("yfinance 返回空数据")
            return []
        candles = []
        for idx, row in df.iterrows():
            candles.append({
                "close_time": int(idx.timestamp() * 1000),
                "open":  float(row["Open"]),
                "high":  float(row["High"]),
                "low":   float(row["Low"]),
                "close": float(row["Close"]),
            })
        now_ms = int(time.time() * 1000)
        complete = [c for c in candles if c["close_time"] < now_ms - 60000]
        if not complete:
            log.warning("未找到已收口 K 线，改用全部 %d 根", len(candles))
            complete = candles
        log.info("从 yfinance 获取到 %d 条 15m K 线 (BTC-USD)", len(complete))
        return complete
    except Exception as e:
        log.error("yfinance 数据获取失败: %s", e)
        import traceback
        log.debug(traceback.format_exc())
        return []

    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    raw = r.json()
    candles = []
    for c in raw:
        candles.append({
            "close_time": c[6],
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
        })
    # 只保留已收口的 K线
    now_ms = int(time.time() * 1000)
    complete = [c for c in candles if c["close_time"] < now_ms - 60000]
    if not complete:
        log.warning("未找到已收口 K线，改用全部")
        complete = candles
    return complete


# ============================================================
# 摆点检测（局部最高/最低）
# ============================================================
def find_pivots(candles, lookback):
    pivots = []
    n = len(candles)
    if n < 2 * lookback + 1:
        return pivots
    for i in range(lookback, n - lookback):
        wh = max(candles[j]["high"] for j in range(i - lookback, i + lookback + 1))
        wl = min(candles[j]["low"]  for j in range(i - lookback, i + lookback + 1))
        if candles[i]["high"] == wh:
            pivots.append({"idx": i, "type": "high", "price": candles[i]["high"]})
        if candles[i]["low"] == wl:
            pivots.append({"idx": i, "type": "low",  "price": candles[i]["low"]})
    return pivots


# ============================================================
# 单一结构的 CHoCH / BOS 检测（带防重复）
# ============================================================
def detect_structure(candles, lookback, state, label):
    """
    state 字典包含:
        trend: 0=未初始化, 1=牛, -1=熊
        last_high, last_high_idx
        last_low,  last_low_idx
        last_triggered_price: 上次触发信号的价格
        last_triggered_time:  上次触发的时间（毫秒）
    返回 (signal, msg) 或 (None, None)
    signal: BULL_BOS | BULL_CHOCH | BEAR_BOS | BEAR_CHOCH
    """
    pivots = find_pivots(candles, lookback)
    if not pivots:
        return None, None

    now_ms = int(time.time() * 1000)

    # 更新最近的前高/前低
    highs = [p for p in pivots if p["type"] == "high"]
    lows  = [p for p in pivots if p["type"] == "low"]

    latest_high = max(highs, key=lambda p: p["idx"], default=None)
    latest_low  = min(lows,  key=lambda p: p["idx"], default=None)

    if latest_high and latest_high["idx"] > state.get("last_high_idx", -1):
        state["last_high"]     = latest_high["price"]
        state["last_high_idx"] = latest_high["idx"]
    if latest_low and latest_low["idx"] > state.get("last_low_idx", -1):
        state["last_low"]      = latest_low["price"]
        state["last_low_idx"]  = latest_low["idx"]

    # 初始化趋势方向
    if state.get("trend", 0) == 0:
        if latest_high and latest_low:
            # 谁先出现谁的极性代表当前趋势
            state["trend"] = -1 if latest_high["idx"] > latest_low["idx"] else 1
        elif latest_high:
            state["trend"] = -1
        elif latest_low:
            state["trend"] = 1
        return None, None

    cur     = candles[-1]["close"]
    cur_idx = len(candles) - 1

    # ---------- 防重复报警 ----------
    last_triggered_price = state.get("last_triggered_price", 0.0)
    last_triggered_time  = state.get("last_triggered_time", 0)

    # 冷却检查
    if (now_ms - last_triggered_time) < COOLDOWN_SEC:
        log.info("[%s] 冷却中（%d 秒内），跳过", label,
                 (now_ms - last_triggered_time) // 1000)
        return None, None

    # 如果上次触发是同方向，且本次价格只是围绕旧位小波动，不报
    if last_triggered_price > 0:
        pct = abs(cur - last_triggered_price) / last_triggered_price * 100
        if pct < MIN_PCT_CHANGE:
            log.info("[%s] 价格 %.2f 与上次触发位 %.2f 偏移 %.2f%% < %.2f%%，不报",
                     label, cur, last_triggered_price, pct, MIN_PCT_CHANGE)
            return None, None

    sig, msg = None, None

    # 向上突破前高
    if cur > state["last_high"]:
        if state["trend"] == 1:
            sig = "BULL_BOS"
            msg = (f"🟢 BTCUSDT 15m {label} 牛市结构延伸 (BOS)\n"
                   f"前高 {state['last_high']:.2f} → 现价 {cur:.2f}")
        else:
            sig = "BEAR_CHOCH"
            msg = (f"🔴 BTCUSDT 15m {label} 熊转牛 (CHoCH)\n"
                   f"突破前高 {state['last_high']:.2f} → 现价 {cur:.2f}\n"
                   f"可能转为牛市，注意观察")
            state["trend"] = 1

        state["last_triggered_price"] = cur
        state["last_triggered_time"]  = now_ms
        state["last_high"]     = cur
        state["last_high_idx"] = cur_idx

    # 向下跌破前低
    elif cur < state["last_low"]:
        if state["trend"] == -1:
            sig = "BEAR_BOS"
            msg = (f"🔴 BTCUSDT 15m {label} 熊市结构延伸 (BOS)\n"
                   f"前低 {state['last_low']:.2f} → 现价 {cur:.2f}")
        else:
            sig = "BULL_CHOCH"
            msg = (f"🟢 BTCUSDT 15m {label} 牛转熊 (CHoCH)\n"
                   f"跌破前低 {state['last_low']:.2f} → 现价 {cur:.2f}\n"
                   f"可能转为熊市，注意观察")
            state["trend"] = -1

        state["last_triggered_price"] = cur
        state["last_triggered_time"]  = now_ms
        state["last_low"]      = cur
        state["last_low_idx"]  = cur_idx

    return sig, msg


# ============================================================
# Gist 状态存取
# ============================================================
def load_state():
    if not USE_GIST:
        return {}
    try:
        r = requests.get(f"{GIST_API}/{GIST_ID}", headers=HEADERS, timeout=30)
        r.raise_for_status()
        files = r.json().get("files", {})
        content = files.get("state.json", {}).get("content", "{}")
        return json.loads(content)
        return json.loads(content)
    except Exception as e:
        log.error("Gist 状态读取失败: %s", e)
        return {}


def save_state(state):
    if not USE_GIST:
        return
    try:
        payload = {"files": {"state.json": {"content": json.dumps(state, indent=2)}}}
        if GIST_ID:
            r = requests.patch(f"{GIST_API}/{GIST_ID}", json=payload, headers=HEADERS, timeout=30)
        else:
            payload["public"] = False
            r = requests.post(GIST_API, json=payload, headers=HEADERS, timeout=30)
            new_id = r.json().get("id", "")
            log.info("新建 Gist: %s — 请将其加入仓库 Secrets 'GIST_ID'", new_id)
        r.raise_for_status()
    except Exception as e:
        log.error("Gist 状态保存失败: %s", e)


# ============================================================
# 通知函数
# ============================================================
def send_telegram(msg):
    if not USE_TG:
        return
    try:
        url = f"<https://api.telegram.org/bot{TG_TOKEN}/sendMessage>"
        r = requests.post(url, json={
            "chat_id": TG_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown",
        }, timeout=30)
        r.raise_for_status()
        log.info("Telegram 消息已发送")
    except Exception as e:
        log.error("Telegram 发送失败: %s", e)


def send_qq_email(msg):
    if not USE_QQ:
        return
    try:
        smtp_server = "smtp.qq.com"
        smtp_port   = 465
        mail = MIMEText(msg, "plain", "utf-8")
        mail["Subject"] = Header("BTC SMC 信号提醒", "utf-8")
        mail["From"]    = QQ_EMAIL
        mail["To"]      = QQ_EMAIL_TO
        with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30) as srv:
            srv.login(QQ_EMAIL, QQ_SMTP_PW)
            srv.sendmail(QQ_EMAIL, [QQ_EMAIL_TO], mail.as_string())
        log.info("QQ邮箱 发送完毕")
    except Exception as e:
        log.error("QQ邮箱 发送失败: %s", e)


def send_serverchui(msg):
    if not USE_SC:
        return
    try:
        url = f"<https://sc.ftqq.com/{SC_SENDKEY}.send>"
        r = requests.post(url, data={"text": "BTC SMC 信号", "desp": msg}, timeout=30)
        r.raise_for_status()
        log.info("Server酏 消息已发送")
    except Exception as e:
        log.error("Server酏 发送失败: %s", e)


# ============================================================
# 主流程
# ============================================================
def main():
    log.info("拉取 %s %s K线 ...", SYMBOL, INTERVAL)
    candles = fetch_klines(SYMBOL, INTERVAL, KLINE_LIMIT)
    log.info("收到 %d 根已收口 K线", len(candles))

    if len(candles) < 2 * max(LOOKBACK_INNER, LOOKBACK_SWING) + 1:
        log.warning("K线太少，跳过本次检测")
        return

    state = load_state()

    inner_state = state.get("inner", {
        "trend": 0,
        "last_high": 0.0, "last_high_idx": -1,
        "last_low": 0.0,  "last_low_idx": -1,
        "last_triggered_price": 0.0, "last_triggered_time": 0,
    })
    swing_state = state.get("swing", {
        "trend": 0,
        "last_high": 0.0, "last_high_idx": -1,
        "last_low": 0.0,  "last_low_idx": -1,
        "last_triggered_price": 0.0, "last_triggered_time": 0,
    })

    log.info("内部结构: trend=%s, last_high=%.2f, last_low=%.2f",
             inner_state.get("trend"), inner_state.get("last_high"), inner_state.get("last_low"))
    log.info("Swing 结构: trend=%s, last_high=%.2f, last_low=%.2f",
             swing_state.get("trend"), swing_state.get("last_high"), swing_state.get("last_low"))

    signals_found = []

    sig_inner, msg_inner = detect_structure(candles, LOOKBACK_INNER, inner_state, "内部结构")
    if sig_inner:
        log.info("[内部结构] 检测到信号: %s", sig_inner)
        log.info(msg_inner)
        signals_found.append(("内部结构", sig_inner, msg_inner))

    sig_swing, msg_swing = detect_structure(candles, LOOKBACK_SWING, swing_state, "Swing 结构")
    if sig_swing:
        log.info("[Swing 结构] 检测到信号: %s", sig_swing)
        log.info(msg_swing)
        signals_found.append(("Swing 结构", sig_swing, msg_swing))

    if signals_found:
        combined_msg = "📊 BTCUSDT 15m SMC 信号\n\n"
        for which, sig, msg in signals_found:
            combined_msg += f"【{which}】\n{msg}\n\n"
        log.info("合并消息:\n%s", combined_msg)

        send_telegram(combined_msg)
        send_qq_email(combined_msg)
        send_serverchui(combined_msg)

        state["inner"]  = inner_state
        state["swing"]  = swing_state
    else:
        log.info("无信号")
        state["inner"]  = inner_state
        state["swing"]  = swing_state

    save_state(state)


if __name__ == "__main__":
    main()