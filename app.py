import os
import time
import threading
import requests
import yfinance as yf
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ── CONFIG ──────────────────────────────────────────
TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHATID = os.environ.get("TG_CHAT_ID", "")

# ── DEFAULT ASSETS ───────────────────────────────────
DEFAULT_ASSETS = [
    {"ticker": "GBPUSD=X",  "name": "GBP / USD",        "type": "forex"},
    {"ticker": "EURUSD=X",  "name": "EUR / USD",        "type": "forex"},
    {"ticker": "JPY=X",     "name": "USD / JPY",        "type": "currency"},
    {"ticker": "GC=F",      "name": "Gold",             "type": "commodity"},
    {"ticker": "SI=F",      "name": "Silver",           "type": "commodity"},
    {"ticker": "CL=F",      "name": "Crude Oil (WTI)",  "type": "commodity"},
    {"ticker": "NG=F",      "name": "Natural Gas",      "type": "commodity"},
]

# ── STATE ────────────────────────────────────────────
assets      = {a["ticker"]: dict(a, trend=None, fib=None, price=None,
                                  price_change=None, alert_triggered=False,
                                  last_scan=None, swing_high=None,
                                  swing_low=None, prev_side=None)
               for a in DEFAULT_ASSETS}

alert_log   = []   # list of dicts
alert_count = 0

# ── HELPERS ──────────────────────────────────────────
def fmt_price(price, ticker, atype):
    if price is None:
        return "—"
    t = ticker.upper()
    if atype in ("forex", "currency") or t.endswith("=X"):
        decimals = 3 if price > 20 else 5
        return f"{price:.{decimals}f}"
    if atype == "commodity":
        return f"${price:.2f}"
    if atype == "crypto" or t.endswith("-USD"):
        if price < 1:   return f"{price:.4f}"
        if price < 100: return f"{price:.3f}"
        return f"${price:,.2f}"
    if t.endswith(".NS"):
        return f"₹{price:.2f}"
    return f"{price:.2f}"


def assess_trend(candles):
    """Last 5 × 4H candles → 'up' or 'down'."""
    if len(candles) < 2:
        return "neutral"
    c = candles[-5:]
    up, dn = 0, 0
    for i in range(1, len(c)):
        if c[i]["h"] > c[i-1]["h"]: up += 2
        else: dn += 2
        if c[i]["l"] > c[i-1]["l"]: up += 2
        else: dn += 2
        if c[i]["c"] > c[i-1]["c"]: up += 1
        else: dn += 1
    return "up" if up >= dn else "down"


def build_fib(from_price, to_price):
    r = to_price - from_price
    return {
        "f0":   from_price,
        "f236": from_price + r * 0.236,
        "f382": from_price + r * 0.382,
        "f50":  from_price + r * 0.5,
        "f618": from_price + r * 0.618,
        "f100": to_price,
    }


def check_direction_alert(asset, price, fib50):
    """
    Uptrend   → alert when price comes DOWN to 50% (was above, now at/below)
    Downtrend → alert when price comes UP   to 50% (was below, now at/above)
    Returns True if alert should fire.
    """
    tolerance = abs(asset["fib"]["f100"] - asset["fib"]["f0"]) * 0.006
    prev_side = asset.get("prev_side")
    cur_side  = "above" if price > fib50 else "below"
    asset["prev_side"] = cur_side

    if not prev_side:
        return False

    if asset["trend"] == "up":
        if prev_side == "above" and cur_side == "below":
            return True
        if cur_side == "below" and abs(price - fib50) <= tolerance:
            return True

    if asset["trend"] == "down":
        if prev_side == "below" and cur_side == "above":
            return True
        if cur_side == "above" and abs(price - fib50) <= tolerance:
            return True

    return False


# ── TELEGRAM ─────────────────────────────────────────
def send_telegram(text):
    token   = TG_TOKEN
    chat_id = TG_CHATID
    if not token or not chat_id:
        return False
    try:
        url  = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


def fire_alert(asset):
    global alert_count
    alert_count += 1

    direction = (
        "📉 Price pulled BACK DOWN to 50% Fib (Uptrend setup)"
        if asset["trend"] == "up"
        else "📈 Price BOUNCED UP to 50% Fib (Downtrend setup)"
    )
    fib50_fmt = fmt_price(asset["fib"]["f50"], asset["ticker"], asset["type"])
    price_fmt = fmt_price(asset["price"],      asset["ticker"], asset["type"])

    msg = "\n".join([
        "🚨 FIBALERT PRO — SETUP TRIGGERED",
        "",
        f"📌 Asset : {asset['name']} ({asset['ticker']})",
        f"{direction}",
        "",
        f"💰 Price     : {price_fmt}",
        f"🎯 50% Fib   : {fib50_fmt}",
        f"📊 Trend (4H): {'▲ UPTREND' if asset['trend']=='up' else '▼ DOWNTREND'}",
        f"📅 Day High  : {fmt_price(asset['swing_high'], asset['ticker'], asset['type'])}",
        f"📅 Day Low   : {fmt_price(asset['swing_low'],  asset['ticker'], asset['type'])}",
        f"⏰ Time      : {datetime.now().strftime('%H:%M:%S')}",
        "",
        "⚡ Check chart & take trade with confirmation!",
    ])
    send_telegram(msg)

    log_entry = {
        "time":      datetime.now().strftime("%H:%M:%S"),
        "ticker":    asset["ticker"],
        "name":      asset["name"],
        "trend":     asset["trend"],
        "direction": direction,
        "price":     price_fmt,
        "fib50":     fib50_fmt,
    }
    alert_log.insert(0, log_entry)
    if len(alert_log) > 100:
        alert_log.pop()


# ── SCANNER ──────────────────────────────────────────
def scan_asset(ticker):
    asset = assets[ticker]
    try:
        # 4H data — trend assessment
        tk4h  = yf.Ticker(ticker)
        df4h  = tk4h.history(period="5d", interval="4h")
        if df4h.empty or len(df4h) < 2:
            return

        candles4h = [
            {"h": row.High, "l": row.Low, "c": row.Close}
            for _, row in df4h.iterrows()
        ]
        trend = assess_trend(candles4h)
        asset["trend"] = trend

        # 15M data — fibonacci
        df15m = tk4h.history(period="1d", interval="15m")
        if df15m.empty:
            return

        day_high = float(df15m["High"].max())
        day_low  = float(df15m["Low"].min())
        cur_price = float(df15m["Close"].iloc[-1])
        prev_price = float(df15m["Close"].iloc[-2]) if len(df15m) > 1 else cur_price

        asset["swing_high"] = day_high
        asset["swing_low"]  = day_low
        asset["price"]      = cur_price
        asset["price_change"] = ((cur_price - prev_price) / prev_price * 100) if prev_price else 0
        asset["last_scan"]  = datetime.now().strftime("%H:%M:%S")

        # Build fib based on trend direction
        fib = build_fib(day_low, day_high) if trend == "up" else build_fib(day_high, day_low)
        asset["fib"] = fib

        # Alert check
        if check_direction_alert(asset, cur_price, fib["f50"]):
            if not asset["alert_triggered"]:
                asset["alert_triggered"] = True
                fire_alert(asset)
        else:
            rng = abs(fib["f100"] - fib["f0"])
            if rng and abs(cur_price - fib["f50"]) > rng * 0.04:
                asset["alert_triggered"] = False

    except Exception as e:
        print(f"Scan error [{ticker}]: {e}")


def scan_all():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(assets)} assets...")
    for ticker in list(assets.keys()):
        scan_asset(ticker)
        time.sleep(0.5)   # gentle rate limiting


# ── ROUTES ───────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/assets")
def api_assets():
    out = []
    for ticker, a in assets.items():
        fib = a.get("fib") or {}
        rng = abs(fib.get("f100", 0) - fib.get("f0", 0))
        fib50 = fib.get("f50")
        price = a.get("price")
        dist_pct = None
        pct_on_bar = 50
        if fib50 is not None and price is not None and rng:
            dist_pct  = round(abs(price - fib50) / rng * 100, 2)
            pct_on_bar = max(1, min(99, (price - fib.get("f0", 0)) / rng * 100))

        out.append({
            "ticker":        ticker,
            "name":          a["name"],
            "type":          a["type"],
            "trend":         a.get("trend"),
            "price":         fmt_price(price, ticker, a["type"]),
            "price_raw":     price,
            "price_change":  round(a.get("price_change") or 0, 2),
            "fib50":         fmt_price(fib50, ticker, a["type"]),
            "f0":            fmt_price(fib.get("f0"),   ticker, a["type"]),
            "f382":          fmt_price(fib.get("f382"), ticker, a["type"]),
            "f618":          fmt_price(fib.get("f618"), ticker, a["type"]),
            "f100":          fmt_price(fib.get("f100"), ticker, a["type"]),
            "swing_high":    fmt_price(a.get("swing_high"), ticker, a["type"]),
            "swing_low":     fmt_price(a.get("swing_low"),  ticker, a["type"]),
            "dist_pct":      dist_pct,
            "pct_on_bar":    round(pct_on_bar, 1),
            "alert":         a.get("alert_triggered", False),
            "last_scan":     a.get("last_scan"),
        })
    return jsonify(out)


@app.route("/api/alerts")
def api_alerts():
    return jsonify(alert_log[:50])


@app.route("/api/stats")
def api_stats():
    up   = sum(1 for a in assets.values() if a.get("trend") == "up")
    down = sum(1 for a in assets.values() if a.get("trend") == "down")
    return jsonify({
        "total":  len(assets),
        "up":     up,
        "down":   down,
        "alerts": alert_count,
    })


@app.route("/api/add_asset", methods=["POST"])
def api_add_asset():
    data   = request.json or {}
    ticker = (data.get("ticker") or "").upper().strip()
    name   = data.get("name") or ticker
    atype  = data.get("type") or "stock"
    if not ticker:
        return jsonify({"ok": False, "error": "No ticker"}), 400
    if ticker in assets:
        return jsonify({"ok": False, "error": "Already in watchlist"}), 400
    assets[ticker] = {
        "ticker": ticker, "name": name, "type": atype,
        "trend": None, "fib": None, "price": None,
        "price_change": None, "alert_triggered": False,
        "last_scan": None, "swing_high": None, "swing_low": None, "prev_side": None,
    }
    threading.Thread(target=scan_asset, args=(ticker,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/remove_asset", methods=["POST"])
def api_remove_asset():
    ticker = (request.json or {}).get("ticker", "").upper()
    if ticker in assets:
        del assets[ticker]
    return jsonify({"ok": True})


@app.route("/api/test_telegram", methods=["POST"])
def api_test_telegram():
    global TG_TOKEN, TG_CHATID
    data = request.json or {}
    TG_TOKEN  = data.get("token",   TG_TOKEN)
    TG_CHATID = data.get("chat_id", TG_CHATID)
    ok = send_telegram(
        "✅ FibAlert Pro is connected!\n\n"
        "You will receive instant alerts here whenever your Fibonacci 50% setup triggers.\n\n"
        "📐 Strategy: 4H trend + 15M Fibonacci\n"
        "🎯 Alert: Price reaches 50% Fib from correct direction\n"
        "⚡ Running 24/7 on cloud server!"
    )
    return jsonify({"ok": ok})


@app.route("/api/save_telegram", methods=["POST"])
def api_save_telegram():
    global TG_TOKEN, TG_CHATID
    data = request.json or {}
    TG_TOKEN  = data.get("token",   "")
    TG_CHATID = data.get("chat_id", "")
    return jsonify({"ok": True})


@app.route("/api/scan_now", methods=["POST"])
def api_scan_now():
    threading.Thread(target=scan_all, daemon=True).start()
    return jsonify({"ok": True})


# ── STARTUP ──────────────────────────────────────────
if __name__ == "__main__":
    # Initial scan
    threading.Thread(target=scan_all, daemon=True).start()

    # Auto-scan every 2 minutes
    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_all, "interval", minutes=2)
    scheduler.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
