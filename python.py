#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 15m SMC Monitor - 币安公开API + QQ邮箱告警
检测 BTCUSDT 15m，内部结构 + Swing 结构的 BOS/CHoCH，触发时发 QQ 邮件
GitHub Actions 每 15 分钟跑一次，免费、云端、长期运行
"""

import os, json, time, logging, smtplib, requests
from email.mime.text import MIMEText
from email.header import Header

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("btc")

# ===== 配置（环境变量优先，否则用下面写死的默认值）=====
SYMBOL       = os.environ.get("SYMBOL", "BTCUSDT")
INTERVAL     = os.environ.get("INTERVAL", "15m")
KLINE_LIMIT  = int(os.environ.get("KLINE_LIMIT", "300"))

LOOKBACK_IN  = int(os.environ.get("LOOKBACK_INNER", "5"))   # 内部结构
LOOKBACK_SW  = int(os.environ.get("LOOKBACK_SWING", "20"))  # Swing 结构

QQ_EMAIL     = os.environ.get("QQ_EMAIL", "2994913023@qq.com")
QQ_PASS      = os.environ.get("QQ_EMAIL_PASSWORD", "bukykubpwaztdeii")
QQ_TO        = os.environ.get("QQ_EMAIL_TO", QQ_EMAIL)

COOLDOWN     = int(os.environ.get("COOLDOWN_SEC", "1800"))
MIN_PCT      = float(os.environ.get("MIN_PCT_CHANGE", "0.3"))


# ===== 币安公开 K线 =====
def fetch_klines(symbol, interval, limit):
    url = "https://api.binance.com/api/v3/klines"
    try:
        r = requests.get(url, params={
            "symbol": symbol, "interval": interval, "limit": limit
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("币安API失败: %s", e)
        return []

    out = []
    for c in data:
        out.append({
            "close_time": c[6],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
        })
    # 剔除尚未收盘的 bar
    now_ms = int(time.time() * 1000)
    done = [c for c in out if c["close_time"] < now_ms - 60000]
    return done if done else out


# ===== 摆点检测 =====
def find_pivots(candles, lookback):
    pvt = []
    n = len(candles)
    if n < 2 * lookback + 1:
        return pvt
    for i in range(lookback, n - lookback):
        hi = max(candles[j]["high"] for j in range(i - lookback, i + lookback + 1))
        lo = min(candles[j]["low"]  for j in range(i - lookback, i + lookback + 1))
        if candles[i]["high"] == hi:
            pvt.append({"idx": i, "type": "high", "price": candles[i]["high"]})
        if candles[i]["low"] == lo:
            pvt.append({"idx": i, "type": "low",  "price": candles[i]["low"]})
    return pvt


# ===== 结构监控：BOS / CHoCH =====
class StructureMonitor:
    def __init__(self, lookback, label):
        self.lb = lookback
        self.label = label
        self.trend = 0         # 0=未初始化, 1=牛, -1=熊
        self.last_high = 0.0
        self.last_low  = 0.0
        self.last_price = 0.0  # 触发时的价格
        self.last_time  = 0    # 触发时的秒数
        self._hi_idx = -1
        self._lo_idx = -1

    def update(self, candles):
        pvt = find_pivots(candles, self.lb)
        if not pvt:
            return None, None

        for p in pvt:
            if p["type"] == "high" and p["idx"] > self._hi_idx:
                self.last_high = p["price"]
                self._hi_idx = p["idx"]
            if p["type"] == "low" and p["idx"] > self._lo_idx:
                self.last_low = p["price"]
                self._lo_idx = p["idx"]

        # 初始化趋势
        if self.trend == 0:
            highs = [p for p in pvt if p["type"] == "high"]
            lows  = [p for p in pvt if p["type"] == "low"]
            if highs and lows:
                self.trend = -1 if highs[0]["idx"] > lows[0]["idx"] else 1
            elif highs:
                self.trend = -1
            elif lows:
                self.trend = 1
            return None, None

        cur  = candles[-1]["close"]
        now  = int(time.time())

        # 防重复：冷却 + 最小变化
        if now - self.last_time < COOLDOWN:
            return None, None
        if self.last_price > 0 and abs(cur - self.last_price) / self.last_price * 100 < MIN_PCT:
            return None, None

        if cur > self.last_high:
            if self.trend == 1:
                sig, msg = "BULL_BOS", f"🟢 BTCUSDT 15m {self.label} 牛BOS\n前高 {self.last_high:,.2f} → 现价 {cur:,.2f}"
            else:
                sig, msg = "BEAR_CHOCH", f"🔴 BTCUSDT 15m {self.label} 熊转牛（CHoCH）\n突破前高 {self.last_high:,.2f} → 现价 {cur:,.2f}\n可能转牛，注意观察"
                self.trend = 1
            self.last_price = cur
            self.last_time  = now
            self.last_high  = cur
            return sig, msg

        if cur < self.last_low:
            if self.trend == -1:
                sig, msg = "BEAR_BOS", f"🔴 BTCUSDT 15m {self.label} 熊BOS\n前低 {self.last_low:,.2f} → 现价 {cur:,.2f}"
            else:
                sig, msg = "BULL_CHOCH", f"🟢 BTCUSDT 15m {self.label} 牛转熊（CHoCH）\n跌破前低 {self.last_low:,.2f} → 现价 {cur:,.2f}\n可能转熊，注意观察"
                self.trend = -1
            self.last_price = cur
            self.last_time  = now
            self.last_low   = cur
            return sig, msg

        return None, None


# ===== QQ 邮箱告警 =====
def send_email(subject, body):
    if not (QQ_EMAIL and QQ_PASS):
        log.warning("邮箱未配置")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"]    = QQ_EMAIL
        msg["To"]      = QQ_TO
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        s.login(QQ_EMAIL, QQ_PASS)
        s.sendmail(QQ_EMAIL, [QQ_TO], msg.as_string())
        s.quit()
        log.info("邮件已发送至 %s", QQ_TO)
    except Exception as e:
        log.error("邮件失败: %s", e)


# ===== state.json 持久化 =====
def load_state():
    try:
        with open("state.json") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(s):
    try:
        with open("state.json", "w") as f:
            json.dump(s, f, indent=2)
    except Exception as e:
        log.warning("写 state.json 失败: %s", e)


# ===== 主流程 =====
def main():
    log.info("拉取 %s %s K线 (limit=%d)...", SYMBOL, INTERVAL, KLINE_LIMIT)
    candles = fetch_klines(SYMBOL, INTERVAL, KLINE_LIMIT)
    log.info("得到 %d 根 K 线", len(candles))

    if len(candles) < 2 * max(LOOKBACK_IN, LOOKBACK_SW) + 1:
        log.warning("K线太少，跳过本次")
        save_state({})
        return

    st = load_state()

    inner = StructureMonitor(LOOKBACK_IN, "内部结构")
    swing = StructureMonitor(LOOKBACK_SW, "Swing结构")

    for k, v in [
        ("trend", "inner_trend"), ("last_high", "inner_last_high"),
        ("last_low", "inner_last_low"), ("last_price", "inner_last_price"),
        ("last_time", "inner_last_time"), ("_hi_idx", "inner_hi_idx"),
        ("_lo_idx", "inner_lo_idx"),
    ]:
        setattr(inner, k, st.get(v, -1 if "idx" in k else 0))

    for k, v in [
        ("trend", "swing_trend"), ("last_high", "swing_last_high"),
        ("last_low", "swing_last_low"), ("last_price", "swing_last_price"),
        ("last_time", "swing_last_time"), ("_hi_idx", "swing_hi_idx"),
        ("_lo_idx", "swing_lo_idx"),
    ]:
        setattr(swing, k, st.get(v, -1 if "idx" in k else 0))

    log.info("内: trend=%s high=%.2f low=%.2f", inner.trend, inner.last_high, inner.last_low)
    log.info("摆: trend=%s high=%.2f low=%.2f", swing.trend, swing.last_high, swing.last_low)

    alerts = []
    for mon in (inner, swing):
        sig, msg = mon.update(candles)
        if sig:
            log.info("[%s] 检测到: %s", mon.label, sig)
            alerts.append((mon.label, sig, msg))

    if alerts:
        body = "BTCUSDT 15m SMC 信号\n\n" + \
               "\n\n".join(f"[{w}] {m}" for w, _, m in alerts) + "\n"
        log.info("发送邮件:\n%s", body)
        send_email("BTC SMC 信号提醒", body)

    st["inner_trend"]      = inner.trend
    st["inner_last_high"]  = inner.last_high
    st["inner_last_low"]   = inner.last_low
    st["inner_last_price"] = inner.last_price
    st["inner_last_time"]  = inner.last_time
    st["inner_hi_idx"]     = inner._hi_idx
    st["inner_lo_idx"]     = inner._lo_idx

    st["swing_trend"]      = swing.trend
    st["swing_last_high"]  = swing.last_high
    st["swing_last_low"]   = swing.last_low
    st["swing_last_price"] = swing.last_price
    st["swing_last_time"]  = swing.last_time
    st["swing_hi_idx"]     = swing._hi_idx
    st["swing_lo_idx"]     = swing._lo_idx

    st["last_update"] = int(time.time())
    save_state(st)
    log.info("运行完成")


if __name__ == "__main__":
    main()
