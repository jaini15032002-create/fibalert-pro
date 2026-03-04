import os
import time
import threading
import requests
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, jsonify, render_template, request
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

TG_TOKEN  = os.environ.get("TG_TOKEN", "")
TG_CHATID = os.environ.get("TG_CHAT_ID", "")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://finance.yahoo.com",
    "Origin": "https://finance.yahoo.com",
})

DEFAULT_ASSETS = [
    {"ticker": "GBPUSD=X",  "name": "GBP / USD",       "type": "forex"},
    {"ticker": "EURUSD=X",  "name": "EUR / USD",       "type": "forex"},
    {"ticker": "JPY=X",     "name": "USD / JPY",       "type": "currency"},
    {"ticker": "GC=F",      "name": "Gold",            "type": "commodity"},
    {"ticker": "SI=F",      "name": "Silver",          "type": "commodity"},
    {"ticker": "CL=F",      "name": "Crude Oil (WTI)", "type": "commodity"},
    {"ticker": "NG=F",      "name": "Natural Gas",     "type": "commodity"},
]

assets = {
    a["ticker"]: dict(a, trend=None, fib=None, price=None,
                      price_change=None, alert_triggered=False,
                      last_scan=None, swing_high=None,
                      swing_low=None, prev_side=None, error=None)
    for a in DEFAULT_ASSETS
}

alert_log   = []
alert_count = 0

def fmt_price(price, ticker, atype):
    if price is None: return "—"
    t = ticker.upper()
    if atype in ("forex", "currency") or t.endswith("=X"):
        decimals = 3 if price > 20 else 5
        return f"{price:.{decimals}f}"
    if atype == "commodity": return f"${price:.2f}"
    if atype == "crypto" or t.endswith("-USD"):
        if price < 1: return f"{price:.4f}"
        if price < 100: return f"{price:.3f}"
        return f"${price:,.2f}"
    if t.endswith(".NS"): return f"₹{price:.2f}"
    return f"{price:.2f}"

def fetch_yahoo(ticker, interval, period, retries=3):
    for attempt in range(retries):
        try:
            base = "query1" if attempt < 2 else "query2"
            url = f"https://{base}.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={period}&includePrePost=false"
            resp = SESSION.get(url, timeout=15)
            data = resp.json()
            result = data.get("chart", {}).get("result", [])
            if not result:
                time.sleep(1); continue
            r = result[0]
            quotes = r.get("indicators", {}).get("quote", [{}])[0]
            highs, lows, closes = quotes.get("high",[]), quotes.get("low",[]), quotes.get("close",[])
            candles = [{"h": float(highs[i]), "l": float(lows[i]), "c": float(closes[i])}
                       for i in range(len(closes)) if closes[i] is not None and highs[i] is not None and lows[i] is not None]
            if candles: return candles
        except Exception as e:
            print(f"Yahoo fetch [{ticker}] attempt {attempt+1}: {e}")
            time.sleep(1.5)
    return None

def assess_trend(candles):
    if len(candles) < 2: return "neutral"
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
    return {"f0": from_price, "f236": from_price+r*0.236, "f382": from_price+r*0.382,
            "f50": from_price+r*0.5, "f618": from_price+r*0.618, "f100": to_price}

def check_direction_alert(asset, price, fib50):
    tolerance = abs(asset["fib"]["f100"] - asset["fib"]["f0"]) * 0.006
    prev_side = asset.get("prev_side")
    cur_side  = "above" if price > fib50 else "below"
    asset["prev_side"] = cur_side
    if not prev_side: return False
    if asset["trend"] == "up":
        if prev_side == "above" and cur_side == "below": return True
        if cur_side == "below" and abs(price - fib50) <= tolerance: return True
    if asset["trend"] == "down":
        if prev_side == "below" and cur_side == "above": return True
        if cur_side == "above" and abs(price - fib50) <= tolerance: return True
    return False

def send_telegram(text):
    if not TG_TOKEN or not TG_CHATID: return False
    try:
        resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                             json={"chat_id": TG_CHATID, "text": text}, timeout=10)
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}"); return False

def fire_alert(asset):
    global alert_count
    alert_count += 1
    direction = ("📉 Price pulled BACK DOWN to 50% Fib (Uptrend setup)"
                 if asset["trend"] == "up" else "📈 Price BOUNCED UP to 50% Fib (Downtrend setup)")
    fib50_fmt = fmt_price(asset["fib"]["f50"], asset["ticker"], asset["type"])
    price_fmt = fmt_price(asset["price"], asset["ticker"], asset["type"])
    msg = "\n".join([
        "🚨 FIBALERT PRO — SETUP TRIGGERED", "",
        f"📌 Asset : {asset['name']} ({asset['ticker']})", f"{direction}", "",
        f"💰 Price     : {price_fmt}",
        f"🎯 50% Fib   : {fib50_fmt}",
        f"📊 Trend (4H): {'▲ UPTREND' if asset['trend']=='up' else '▼ DOWNTREND'}",
        f"📅 Day High  : {fmt_price(asset['swing_high'], asset['ticker'], asset['type'])}",
        f"📅 Day Low   : {fmt_price(asset['swing_low'],  asset['ticker'], asset['type'])}",
        f"⏰ Time      : {datetime.now().strftime('%H:%M:%S')}", "",
        "⚡ Check chart & take trade with confirmation!",
    ])
    send_telegram(msg)
    alert_log.insert(0, {"time": datetime.now().strftime("%H:%M:%S"), "ticker": asset["ticker"],
                          "name": asset["name"], "trend": asset["trend"],
                          "direction": direction, "price": price_fmt, "fib50": fib50_fmt})
    if len(alert_log) > 100: alert_log.pop()

def scan_asset(ticker):
    asset = assets.get(ticker)
    if not asset: return
    try:
        candles4h = fetch_yahoo(ticker, "4h", "5d")
        if not candles4h or len(candles4h) < 2:
            asset["error"] = "No 4H data from Yahoo Finance"; return
        trend = assess_trend(candles4h)
        asset["trend"] = trend
        candles15m = fetch_yahoo(ticker, "15m", "1d")
        if not candles15m:
            asset["error"] = "No 15M data from Yahoo Finance"; return
        day_high  = max(c["h"] for c in candles15m)
        day_low   = min(c["l"] for c in candles15m)
        cur_price = candles15m[-1]["c"]
        prev_price = candles15m[-2]["c"] if len(candles15m) > 1 else cur_price
        asset.update({"swing_high": day_high, "swing_low": day_low, "price": cur_price,
                      "price_change": ((cur_price-prev_price)/prev_price*100) if prev_price else 0,
                      "last_scan": datetime.now().strftime("%H:%M:%S"), "error": None})
        fib = build_fib(day_low, day_high) if trend == "up" else build_fib(day_high, day_low)
        asset["fib"] = fib
        if check_direction_alert(asset, cur_price, fib["f50"]):
            if not asset["alert_triggered"]:
                asset["alert_triggered"] = True
                fire_alert(asset)
        else:
            rng = abs(fib["f100"] - fib["f0"])
            if rng and abs(cur_price - fib["f50"]) > rng * 0.04:
                asset["alert_triggered"] = False
        print(f"[{ticker}] ✅ {fmt_price(cur_price, ticker, asset['type'])} | {trend.upper()} | 50%={fmt_price(fib['f50'], ticker, asset['type'])}")
    except Exception as e:
        asset["error"] = str(e)
        print(f"[{ticker}] ❌ {e}")

def scan_all():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Scanning {len(assets)} assets...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(scan_asset, t): t for t in list(assets.keys())}
        for f in as_completed(futures):
            try: f.result()
            except Exception as e: print(f"Thread error: {e}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Scan complete.\n")

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/assets")
def api_assets():
    out = []
    for ticker, a in assets.items():
        fib = a.get("fib") or {}
        rng = abs(fib.get("f100",0) - fib.get("f0",0))
        fib50 = fib.get("f50"); price = a.get("price")
        dist_pct = None; pct_on_bar = 50
        if fib50 is not None and price is not None and rng:
            dist_pct = round(abs(price-fib50)/rng*100, 2)
            pct_on_bar = max(1, min(99, (price-fib.get("f0",0))/rng*100))
        out.append({"ticker": ticker, "name": a["name"], "type": a["type"],
                    "trend": a.get("trend"), "price": fmt_price(price, ticker, a["type"]),
                    "price_change": round(a.get("price_change") or 0, 2),
                    "fib50": fmt_price(fib50, ticker, a["type"]),
                    "f0": fmt_price(fib.get("f0"), ticker, a["type"]),
                    "f382": fmt_price(fib.get("f382"), ticker, a["type"]),
                    "f618": fmt_price(fib.get("f618"), ticker, a["type"]),
                    "f100": fmt_price(fib.get("f100"), ticker, a["type"]),
                    "swing_high": fmt_price(a.get("swing_high"), ticker, a["type"]),
                    "swing_low": fmt_price(a.get("swing_low"), ticker, a["type"]),
                    "dist_pct": dist_pct, "pct_on_bar": round(pct_on_bar,1),
                    "alert": a.get("alert_triggered", False),
                    "last_scan": a.get("last_scan"), "error": a.get("error")})
    return jsonify(out)

@app.route("/api/alerts")
def api_alerts(): return jsonify(alert_log[:50])

@app.route("/api/stats")
def api_stats():
    return jsonify({"total": len(assets),
                    "up": sum(1 for a in assets.values() if a.get("trend")=="up"),
                    "down": sum(1 for a in assets.values() if a.get("trend")=="down"),
                    "alerts": alert_count})

@app.route("/api/add_asset", methods=["POST"])
def api_add_asset():
    data = request.json or {}
    ticker = (data.get("ticker") or "").upper().strip()
    if not ticker: return jsonify({"ok": False, "error": "No ticker"}), 400
    if ticker in assets: return jsonify({"ok": False, "error": "Already in watchlist"}), 400
    assets[ticker] = {"ticker": ticker, "name": data.get("name") or ticker,
                      "type": data.get("type") or "stock", "trend": None, "fib": None,
                      "price": None, "price_change": None, "alert_triggered": False,
                      "last_scan": None, "swing_high": None, "swing_low": None,
                      "prev_side": None, "error": None}
    threading.Thread(target=scan_asset, args=(ticker,), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/remove_asset", methods=["POST"])
def api_remove_asset():
    ticker = (request.json or {}).get("ticker","").upper()
    if ticker in assets: del assets[ticker]
    return jsonify({"ok": True})

@app.route("/api/test_telegram", methods=["POST"])
def api_test_telegram():
    global TG_TOKEN, TG_CHATID
    data = request.json or {}
    TG_TOKEN = data.get("token", TG_TOKEN)
    TG_CHATID = data.get("chat_id", TG_CHATID)
    ok = send_telegram("✅ FibAlert Pro is connected!\n\nYou will receive instant alerts whenever your Fibonacci 50% setup triggers.\n\n📐 4H trend + 15M Fibonacci\n🎯 50% Fib alert from correct direction\n⚡ Running 24/7 on cloud!")
    return jsonify({"ok": ok})

@app.route("/api/save_telegram", methods=["POST"])
def api_save_telegram():
    global TG_TOKEN, TG_CHATID
    data = request.json or {}
    TG_TOKEN = data.get("token",""); TG_CHATID = data.get("chat_id","")
    return jsonify({"ok": True})

@app.route("/api/scan_now", methods=["POST"])
def api_scan_now():
    threading.Thread(target=scan_all, daemon=True).start()
    return jsonify({"ok": True})

if __name__ == "__main__":
    threading.Thread(target=scan_all, daemon=True).start()
    scheduler = BackgroundScheduler()
    scheduler.add_job(scan_all, "interval", minutes=2)
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
