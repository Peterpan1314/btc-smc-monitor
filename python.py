#!/usr/bin/env python3
import os, json, time, logging, smtplib, requests
from email.mime.text import MIMEText
from email.header import Header

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("btc")

# === 配置（对接你已有的 Secrets 名称）===
QQ_EMAIL    = os.environ.get("QQ_EMAIL", "")
QQ_PASS     = os.environ.get("QQ_EMAIL_PASSWORD", "")
QQ_TO       = os.environ.get("QQ_EMAIL_TO", "")
EMAIL_USER  = QQ_EMAIL
EMAIL_PASS  = QQ_PASS
EMAIL_TO    = QQ_TO if QQ_TO else QQ_EMAIL

COOLDOWN    = int(os.environ.get("COOLDOWN_SEC", "1800"))
MIN_PCT     = float(os.environ.get("MIN_PCT_CHANGE", "0.3"))

# === 数据获取：CoinGecko 公开 market_chart（无需 key）===
def fetch_klines(symbol, interval, limit):
    url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    params = {"vs_currency": "usd", "days": "3", "interval": "15m"}
    try:
        r = requests.get(url, params=params, timeout=40)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.error("CoinGecko 拉取失败: %s", e)
        return []

    prices = data.get("prices", [])
    now_ms = int(time.time() * 1000)
    candles = []
    for ts, price in prices:
        close_ms = int(ts)
        candles.append({
            "close_time": close_ms,
            "open":  price,
            "high":  price,
            "low":   price,
            "close": price,
        })
    complete = [c for c in candles if c["close_time"] < now_ms - 60000]
    return (complete if complete else candles)[-limit:]

# === 摆点检测===
def find_pivots(candles, lookback):
    pivots = []
    n = len(candles)
    if n < 2 * lookback + 1:
        return pivots
    for i in range(lookback, n - lookback):
        hi = max(candles[j]["high"] for j in range(i - lookback, i + lookback + 1))
        lo = min(candles[j]["low"]  for j in range(i - lookback, i + lookback + 1))
        if candles[i]["high"] == hi:
            pivots.append({"idx": i, "type": "high", "price": candles[i]["high"]})
        if candles[i]["low"] == lo:
            pivots.append({"idx": i, "type": "low",  "price": candles[i]["low"]})
    return pivots

# === 单一结构监控：BOS / CHoCH（带防重复）===
class StructureMonitor:
    def __init__(self, lookback, label):
        self.lb = lookback
        self.label = label
        self.trend = 0
        self.last_high = 0.0
        self.last_low  = 0.0
        self.triggered_price = 0.0
        self.triggered_time  = 0
        self._hi_idx = -1
        self._lo_idx = -1

    def update(self, candles):
        pivots = find_pivots(candles, self.lb)
        if not pivots:
            return None, None
        for p in pivots:
            if p["type"] == "high" and p["idx"] > self._hi_idx:
                self.last_high = p["price"]; self._hi_idx = p["idx"]
            if p["type"] == "low" and p["idx"] > self._lo_idx:
                self.last_low = p["price"]; self._lo_idx = p["idx"]
        if self.trend == 0:
            highs = [p for p in pivots if p["type"] == "high"]
            lows  = [p for p in pivots if p["type"] == "low"]
            if highs and lows:
                self.trend = -1 if highs[0]["idx"] > lows[0]["idx"] else 1
            elif highs: self.trend = -1
            elif lows:  self.trend = 1
            return None, None
        cur = candles[-1]["close"]
        now = int(time.time())
        if now - self.triggered_time < COOLDOWN:
            return None, None
        if self.triggered_price > 0 and abs(cur - self.triggered_price) / self.triggered_price * 100 < MIN_PCT:
            return None, None
        if cur > self.last_high:
            if self.trend == 1:
                sig, msg = "BULL_BOS", f"🟢 BTCUSDT 15m {self.label} 牛BOS\n前高 {self.last_high:,.2f} → 现价 {cur:,.2f}"
            else:
                sig, msg = "BEAR_CHOCH", f"🔴 BTCUSDT 15m {self.label} 熊转牛（CHoCH）\n突破前高 {self.last_high:,.2f} → 现价 {cur:,.2f}\n可能转牛，注意观察"
                self.trend = 1
            self.triggered_price = cur; self.triggered_time = now; self.last_high = cur
            return sig, msg
        if cur < self.last_low:
            if self.trend == -1:
                sig, msg = "BEAR_BOS", f"🔴 BTCUSDT 15m {self.label} 熊BOS\n前低 {self.last_low:,.2f} → 现价 {cur:,.2f}"
            else:
                sig, msg = "BULL_CHOCH", f"🟢 BTCUSDT 15m {self.label} 牛转熊（CHoCH）\n跌破前低 {self.last_low:,.2f} → 现价 {cur:,.2f}\n可能转熊，注意观察"
                self.trend = -1
            self.triggered_price = cur; self.triggered_time = now; self.last_low = cur
            return sig, msg
        return None, None

# === QQ邮箱告警===
def send_email(subject, body):
    if not (EMAIL_USER and EMAIL_PASS):
        log.warning("邮箱未配置"); return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"]    = EMAIL_USER
        msg["To"]      = EMAIL_TO
        s = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        s.login(EMAIL_USER, EMAIL_PASS)
        s.sendmail(EMAIL_USER, [EMAIL_TO], msg.as_string())
        s.quit()
        log.info("邮件已发送至 %s", EMAIL_TO)
        return True
    except Exception as e:
        log.error("邮件失败: %s", e)
        return False

# === 跨运行状态持久化（state.json）===
def load_state():
    try:
        with open("state.json") as f: return json.load(f)
    except: return {}

def save_state(s):
    try:
        with open("state.json", "w") as f: json.dump(s, f, indent=2)
    except Exception as e: log.warning("写 state.json 失败: %s", e)

def restore(mon, prefix):
    st = load_state()
    for k, sk in [("trend","trend"),("last_high","last_high"),("last_low","last_low"),
                  ("triggered_price","triggered_price"),("triggered_time","triggered_time"),
                  ("_hi_idx","hi_idx"),("_lo_idx","lo_idx")]:
        setattr(mon, k, st.get(f"{prefix}_{sk}", -1 if "idx" in sk else 0))

def persist(mon, prefix):
    st = load_state()
    for k, sk in [("trend","trend"),("last_high","last_high"),("last_low","last_low"),
                  ("triggered_price","triggered_price"),("triggered_time","triggered_time"),
                  ("_hi_idx","hi_idx"),("_lo_idx","lo_idx")]:
        st[f"{prefix}_{sk}"] = getattr(mon, k)
    st["last_update"] = int(time.time())
    save_state(st)

# === 主流程===
def main():
    log.info("拉取 BTC-USD 数据（CoinGecko）...")
    candles = fetch_klines("BTCUSDT", "15m", 300)
    log.info("得到 %d 根 K 线", len(candles))
    if len(candles) < 41:
        log.warning("数据太少，跳过"); return
    inner = StructureMonitor(5,  "内部结构")
    swing = StructureMonitor(20, "Swing 结构")
    restore(inner, "inner"); restore(swing, "swing")
    log.info("内：trend=%-2s high=%s low=%s", inner.trend, inner.last_high or "-", inner.last_low or "-")
    log.info("摆：trend=%-2s high=%s low=%s", swing.trend, swing.last_high or "-", swing.last_low or "-")
    alerts = []
    for mon in (inner, swing):
        sig, msg = mon.update(candles)
        if sig: log.info("[%s] %s", mon.label, sig); alerts.append((mon.label, sig, msg))
    if alerts:
        body = "BTCUSDT 15m SMC 信号\n\n" + "\n\n".join(f"[{哪}] {文}" for 哪, _, 文 in alerts) + "\n"
        log.info("发邮件:\n%s", body)
        send_email("BTC SMC 信号提醒", body)
    persist(inner, "inner"); persist(swing, "swing")
    log.info("运行完成")

if __name__ == "__main__":
    main()
