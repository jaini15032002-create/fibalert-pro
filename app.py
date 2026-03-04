import os, time, threading, requests
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
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
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
    if atype in ("forex","currency") or t.endswith("=X"):
        return f"{price:.3f}" if price > 20 else f"{price:.5f}"
    if atype == "commodity": return f"${price:.2f}"
    if atype == "crypto" or t.endswith("-USD"):
        if price < 1: return f"{price:.4f}"
        if price < 100: return f"{price:.3f}"
        return f"${price:,.2f}"
    if t.endswith(".NS"): return f"Rs.{price:.2f}"
    return f"{price:.2f}"

def fetch_ohlc(ticker, interval, period):
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={period}&includePrePost=false",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={period}&includePrePost=false",
    ]
    for url in urls:
        try:
            r = SESSION.get(url, timeout=20)
            if r.status_code != 200:
                continue
            data = r.json()
            result = data.get("chart", {}).get("result") or []
            if not result:
                continue
            q = result[0].get("indicators", {}).get("quote", [{}])[0]
            H, L, C = q.get("high",[]), q.get("low",[]), q.get("close",[])
            candles = [
                {"h": float(H[i]), "l": float(L[i]), "c": float(C[i])}
                for i in range(len(C))
                if C[i] is not None and H[i] is not None and L[i] is not None
            ]
            if candles:
                return candles
        except Exception as e:
            print(f"  fetch_ohlc [{ticker}] {url[-30:]}: {e}")
        time.sleep(0.5)
    return None

def assess_trend(candles):
    if len(candles) < 2: return "neutral"
    c = candles[-5:]
    up = dn = 0
    for i in range(1, len(c)):
        up += 2 if c[i]["h"] > c[i-1]["h"] else 0
        dn += 2 if c[i]["h"] < c[i-1]["h"] else 0
        up += 2 if c[i]["l"] > c[i-1]["l"] else 0
        dn += 2 if c[i]["l"] < c[i-1]["l"] else 0
        up += 1 if c[i]["c"] > c[i-1]["c"] else 0
        dn += 1 if c[i]["c"] < c[i-1]["c"] else 0
    return "up" if up >= dn else "down"

def build_fib(f, t):
    r = t - f
    return {"f0":f,"f236":f+r*.236,"f382":f+r*.382,"f50":f+r*.5,"f618":f+r*.618,"f100":t}

def check_alert(asset, price, fib50):
    tol = abs(asset["fib"]["f100"] - asset["fib"]["f0"]) * 0.006
    prev = asset.get("prev_side")
    cur  = "above" if price > fib50 else "below"
    asset["prev_side"] = cur
    if not prev: return False
    if asset["trend"] == "up":
        return (prev=="above" and cur=="below") or (cur=="below" and abs(price-fib50)<=tol)
    if asset["trend"] == "down":
        return (prev=="below" and cur=="above") or (cur=="above" and abs(price-fib50)<=tol)
    return False

def send_tg(text):
    if not TG_TOKEN or not TG_CHATID: return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                          json={"chat_id":TG_CHATID,"text":text}, timeout=10)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}"); return False

def fire_alert(asset):
    global alert_count
    alert_count += 1
    d = ("📉 Price pulled BACK DOWN to 50% Fib (Uptrend setup)"
         if asset["trend"]=="up" else "📈 Price BOUNCED UP to 50% Fib (Downtrend setup)")
    fp = fmt_price(asset["price"], asset["ticker"], asset["type"])
    f5 = fmt_price(asset["fib"]["f50"], asset["ticker"], asset["type"])
    send_tg("\n".join([
        "🚨 FIBALERT PRO — SETUP TRIGGERED","",
        f"📌 {asset['name']} ({asset['ticker']})",d,"",
        f"💰 Price   : {fp}",
        f"🎯 50% Fib : {f5}",
        f"📊 Trend   : {'▲ UPTREND' if asset['trend']=='up' else '▼ DOWNTREND'}",
        f"📅 High    : {fmt_price(asset['swing_high'],asset['ticker'],asset['type'])}",
        f"📅 Low     : {fmt_price(asset['swing_low'],asset['ticker'],asset['type'])}",
        f"⏰ Time    : {datetime.now().strftime('%H:%M:%S')}","",
        "⚡ Check chart and take trade with confirmation!",
    ]))
    alert_log.insert(0, {"time":datetime.now().strftime("%H:%M:%S"),
                          "ticker":asset["ticker"],"name":asset["name"],
                          "trend":asset["trend"],"price":fp,"fib50":f5})
    if len(alert_log) > 100: alert_log.pop()

def scan_asset(ticker):
    a = assets.get(ticker)
    if not a: return
    try:
        print(f"  Scanning {ticker}...")
        c4h = fetch_ohlc(ticker, "4h", "5d")
        if not c4h or len(c4h) < 2:
            a["error"] = "No 4H data"; print(f"  [{ticker}] No 4H data"); return
        trend = assess_trend(c4h)
        a["trend"] = trend

        c15 = fetch_ohlc(ticker, "15m", "1d")
        if not c15:
            a["error"] = "No 15M data"; print(f"  [{ticker}] No 15M data"); return

        hi = max(c["h"] for c in c15)
        lo = min(c["l"] for c in c15)
        pr = c15[-1]["c"]
        pp = c15[-2]["c"] if len(c15)>1 else pr

        a.update({"swing_high":hi,"swing_low":lo,"price":pr,
                  "price_change":((pr-pp)/pp*100) if pp else 0,
                  "last_scan":datetime.now().strftime("%H:%M:%S"),"error":None})

        fib = build_fib(lo, hi) if trend=="up" else build_fib(hi, lo)
        a["fib"] = fib

        if check_alert(a, pr, fib["f50"]):
            if not a["alert_triggered"]:
                a["alert_triggered"] = True
                fire_alert(a)
        else:
            rng = abs(fib["f100"]-fib["f0"])
            if rng and abs(pr-fib["f50"]) > rng*0.04:
                a["alert_triggered"] = False

        print(f"  [{ticker}] OK price={fmt_price(pr,ticker,a['type'])} trend={trend}")
    except Exception as e:
        a["error"] = str(e); print(f"  [{ticker}] ERROR: {e}")

def scan_all():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] --- Scanning {len(assets)} assets ---")
    with ThreadPoolExecutor(max_workers=3) as ex:
        fs = {ex.submit(scan_asset, t): t for t in list(assets.keys())}
        for f in as_completed(fs):
            try: f.result()
            except Exception as e: print(f"Thread err: {e}")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] --- Scan done ---\n")

@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/assets")
def api_assets():
    out = []
    for t, a in assets.items():
        fib = a.get("fib") or {}
        rng = abs(fib.get("f100",0)-fib.get("f0",0))
        f50 = fib.get("f50"); p = a.get("price")
        dp = None; pb = 50
        if f50 and p and rng:
            dp = round(abs(p-f50)/rng*100, 2)
            pb = max(1, min(99, (p-fib.get("f0",0))/rng*100))
        out.append({"ticker":t,"name":a["name"],"type":a["type"],
                    "trend":a.get("trend"),
                    "price":fmt_price(p,t,a["type"]),
                    "price_change":round(a.get("price_change") or 0,2),
                    "fib50":fmt_price(f50,t,a["type"]),
                    "f0":fmt_price(fib.get("f0"),t,a["type"]),
                    "f382":fmt_price(fib.get("f382"),t,a["type"]),
                    "f618":fmt_price(fib.get("f618"),t,a["type"]),
                    "f100":fmt_price(fib.get("f100"),t,a["type"]),
                    "swing_high":fmt_price(a.get("swing_high"),t,a["type"]),
                    "swing_low":fmt_price(a.get("swing_low"),t,a["type"]),
                    "dist_pct":dp,"pct_on_bar":round(pb,1),
                    "alert":a.get("alert_triggered",False),
                    "last_scan":a.get("last_scan"),"error":a.get("error")})
    return jsonify(out)

@app.route("/api/alerts")
def api_alerts(): return jsonify(alert_log[:50])

@app.route("/api/stats")
def api_stats():
    return jsonify({"total":len(assets),
                    "up":sum(1 for a in assets.values() if a.get("trend")=="up"),
                    "down":sum(1 for a in assets.values() if a.get("trend")=="down"),
                    "alerts":alert_count})

@app.route("/api/add_asset", methods=["POST"])
def api_add_asset():
    d = request.json or {}
    t = (d.get("ticker") or "").upper().strip()
    if not t: return jsonify({"ok":False,"error":"No ticker"}),400
    if t in assets: return jsonify({"ok":False,"error":"Already in watchlist"}),400
    assets[t] = {"ticker":t,"name":d.get("name") or t,"type":d.get("type") or "stock",
                 "trend":None,"fib":None,"price":None,"price_change":None,
                 "alert_triggered":False,"last_scan":None,"swing_high":None,
                 "swing_low":None,"prev_side":None,"error":None}
    threading.Thread(target=scan_asset, args=(t,), daemon=True).start()
    return jsonify({"ok":True})

@app.route("/api/remove_asset", methods=["POST"])
def api_remove_asset():
    t = (request.json or {}).get("ticker","").upper()
    if t in assets: del assets[t]
    return jsonify({"ok":True})

@app.route("/api/test_telegram", methods=["POST"])
def api_test_telegram():
    global TG_TOKEN, TG_CHATID
    d = request.json or {}
    TG_TOKEN = d.get("token", TG_TOKEN)
    TG_CHATID = d.get("chat_id", TG_CHATID)
    ok = send_tg("✅ FibAlert Pro connected!\n\nAlerts will arrive here when 50% Fibonacci setup triggers.\n📐 4H Trend + 15M Fibonacci\n⚡ Running 24/7 on cloud!")
    return jsonify({"ok":ok})

@app.route("/api/save_telegram", methods=["POST"])
def api_save_telegram():
    global TG_TOKEN, TG_CHATID
    d = request.json or {}
    TG_TOKEN = d.get("token",""); TG_CHATID = d.get("chat_id","")
    return jsonify({"ok":True})

@app.route("/api/scan_now", methods=["POST"])
def api_scan_now():
    threading.Thread(target=scan_all, daemon=True).start()
    return jsonify({"ok":True})

scheduler = BackgroundScheduler()
scheduler.add_job(scan_all, "interval", minutes=2)
scheduler.start()
threading.Thread(target=scan_all, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
