#!/usr/bin/env python3
"""
Binance Futures — Full Criteria Alert (Gmail)

Alert SIRF tab jab coin ye SAB pass kare:
  1. Resistance (7 din ka high) break, aur 8% se zyada upar nahi
  2. Volume >= 10x (pichle 7 din ke hourly average se)
  3. Breakout se pehle daily RSI(14) 45..82
  4. Breakout se pehle 6 din se uptrend
  5. Funding positive
  6. OI (24h) girta nahi

Usage:
  python scanner.py                 # ek baar scan (GitHub Actions yehi chalata hai)
  python scanner.py --loop 60       # har 60 sec scan (apne PC / VPS pe)
  python scanner.py --test-email    # sirf test email bhejo
  python scanner.py --dry           # email mat bhejo, sirf print karo
"""
import os, sys, json, time, smtplib, ssl
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import requests


def _f(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else float(default)


CFG = {
    "base": (os.getenv("BINANCE_BASE") or "https://fapi.binance.com").rstrip("/"),
    "rsi_min": _f("RSI_MIN", 45),
    "rsi_max": _f("RSI_MAX", 82),
    "trend_days": int(_f("TREND_DAYS", 6)),
    "oi_min": _f("OI_MIN", 0),
    "res_days": int(_f("RES_DAYS", 7)),
    "vol_mult": _f("VOL_MULT", 10),
    "max_above": _f("MAX_ABOVE", 8),
    "min_qvol": _f("MIN_QVOL", 1_000_000),
    "cooldown_h": _f("COOLDOWN_H", 6),
}
STATE_FILE = os.getenv("STATE_FILE") or "state.json"
SESSION = requests.Session()


class Blocked(Exception):
    """Binance ne is IP/location ko block kiya (HTTP 451/403)."""


# ---------------------------------------------------------------- API
def api(path, retries=4):
    for i in range(retries):
        try:
            r = SESSION.get(CFG["base"] + path, timeout=25)
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(2)
            continue
        if r.status_code in (451, 403):
            raise Blocked(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code in (418, 429):
            time.sleep(int(r.headers.get("Retry-After", "20")))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("rate limited")


# ---------------------------------------------------------------- analysis
def rsi14(c):
    if len(c) < 15:
        return None
    g = l = 0.0
    for i in range(1, 15):
        d = c[i] - c[i - 1]
        if d >= 0:
            g += d
        else:
            l -= d
    g /= 14
    l /= 14
    for i in range(15, len(c)):
        d = c[i] - c[i - 1]
        g = (g * 13 + max(d, 0)) / 14
        l = (l * 13 + max(-d, 0)) / 14
    return 100.0 if l == 0 else 100 - 100 / (1 + g / l)


def daily_closes(k):
    days, cur = [], None
    for x in k:
        d = int(x[0]) // 86_400_000
        if d != cur:
            days.append([float(x[4]), 1])
            cur = d
        else:
            days[-1][0] = float(x[4])
            days[-1][1] += 1
    if days and days[0][1] < 24:       # adhoora pehla din hata do
        days.pop(0)
    return [d[0] for d in days]


def analyze(sym, k, fund, cfg=CFG):
    """k = 1h klines (aakhri candle abhi chal rahi hai). RSI/uptrend breakout SE PEHLE ki halat pe naapi jati hai."""
    n, win = len(k), cfg["res_days"] * 24
    if n < win + 30:
        return None
    H = [float(x[2]) for x in k]
    Q = [float(x[7]) for x in k]
    C = [float(x[4]) for x in k]
    lo, hi = n - 2 - win, n - 2                      # aakhri 2 ghante resistance window se bahar
    res = max(H[lo:hi])
    base_vol = sum(Q[lo:hi]) / (hi - lo)
    price = C[-1]
    spike = max(Q[-1], Q[-2]) / (base_vol or 1)
    above = (price / res - 1) * 100
    rsi = rsi14(daily_closes(k[: n - 2]))
    back = n - 3 - cfg["trend_days"] * 24
    trend = (C[n - 3] / C[back] - 1) * 100 if back >= 0 else None
    c = {
        "brk": price > res and above <= cfg["max_above"],
        "vol": spike >= cfg["vol_mult"],
        "rsi": rsi is not None and cfg["rsi_min"] <= rsi <= cfg["rsi_max"],
        "up": trend is not None and trend > 0,
        "fund": fund > 0,
        "oi": False,
    }
    return {"sym": sym, "price": price, "res": res, "above": above, "rsi": rsi,
            "trend": trend, "fund": fund, "spike": spike, "oi_chg": None, "c": c}


def pre_ok(r):
    c = r["c"]
    return c["brk"] and c["vol"] and c["rsi"] and c["up"] and c["fund"]


def fmt(p):
    return f"{p:.3f}" if p >= 1 else f"{p:.5f}" if p >= 0.01 else f"{p:.4g}"


# ---------------------------------------------------------------- email
def send_mail(subject, body):
    user = os.getenv("GMAIL_USER")
    pw = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = os.getenv("ALERT_TO") or user
    if not user or not pw:
        print("!! GMAIL_USER / GMAIL_APP_PASSWORD set nahi hain — email nahi gayi.")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"email bhej di -> {to}: {subject}")
    return True


def alert_body(hits):
    parts = []
    for r in hits:
        s = r["sym"]
        parts.append(
            f"{s}\n"
            f"  Resistance {fmt(r['res'])} toota | Price {fmt(r['price'])} (+{r['above']:.1f}%)\n"
            f"  Volume {r['spike']:.1f}x | RSI(1d, pehle) {r['rsi']:.0f} | {CFG['trend_days']}d trend {r['trend']:+.1f}%\n"
            f"  OI 24h {r['oi_chg']:+.1f}% | Funding {r['fund'] * 100:.4f}%\n"
            f"  Binance: https://www.binance.com/en/futures/{s}\n"
            f"  TradingView: https://www.tradingview.com/chart/?symbol=BINANCE:{s}.P\n"
        )
    return ("Sab criteria pass hue (resistance break + volume + RSI + uptrend + funding + OI).\n\n"
            + "\n".join(parts)
            + "\nNote: ye pattern hai, guarantee nahi. Stop-loss zaroor lagao.\n")


# ---------------------------------------------------------------- scan
def scan_once(state, dry=False):
    info = api("/fapi/v1/exchangeInfo")
    tick = api("/fapi/v1/ticker/24hr")
    prem = api("/fapi/v1/premiumIndex")
    ok = {s["symbol"] for s in info["symbols"]
          if s["contractType"] == "PERPETUAL" and s["status"] == "TRADING" and s["quoteAsset"] == "USDT"}
    funding = {p["symbol"]: float(p["lastFundingRate"]) for p in prem}
    syms = [t["symbol"] for t in tick if t["symbol"] in ok and float(t["quoteVolume"]) >= CFG["min_qvol"]]

    def work(sym):
        try:
            k = api(f"/fapi/v1/klines?symbol={sym}&interval=1h&limit=499")
            return analyze(sym, k, funding.get(sym, 0.0))
        except Blocked:
            raise
        except Exception as e:  # ek coin fail ho to baaki chalte rahein
            print(f"skip {sym}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = [r for r in ex.map(work, syms) if r]

    cand = [r for r in out if pre_ok(r)]
    for r in cand:                                   # OI sirf unke liye jinhone baaki sab pass kiya
        try:
            h = api(f"/futures/data/openInterestHist?symbol={r['sym']}&period=1h&limit=25")
            if len(h) > 1:
                r["oi_chg"] = (float(h[-1]["sumOpenInterestValue"]) / float(h[0]["sumOpenInterestValue"]) - 1) * 100
                r["c"]["oi"] = r["oi_chg"] >= CFG["oi_min"]
        except Blocked:
            raise
        except Exception as e:
            print(f"OI skip {r['sym']}: {e}")

    hits = sorted([r for r in cand if r["c"]["oi"]], key=lambda r: -r["spike"])
    now = time.time()
    alerts = state.setdefault("alerts", {})
    fresh = [r for r in hits if now - alerts.get(r["sym"], 0) > CFG["cooldown_h"] * 3600]
    print(f"{len(syms)} coins scan hue | {len(cand)} OI se pehle pass | {len(hits)} sab pass | {len(fresh)} naye alert")
    for r in hits:
        print("  HIT", r["sym"], f"vol {r['spike']:.1f}x above {r['above']:.1f}% rsi {r['rsi']:.0f} oi {r['oi_chg']:+.1f}%")
    if fresh:
        names = ", ".join(r["sym"] for r in fresh)
        subject = f"🚨 {names} — Binance sab criteria pass"
        if dry:
            print(alert_body(fresh))
        elif send_mail(subject, alert_body(fresh)):
            for r in fresh:
                alerts[r["sym"]] = now
    return hits


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def run_once(dry=False):
    state = load_state()
    try:
        scan_once(state, dry)
    except Blocked as e:
        print(f"!! Binance ne block kiya: {e}")
        if time.time() - state.get("err_mail", 0) > 24 * 3600 and not dry:
            body = ("Binance API is server ko block kar rahi hai (HTTP 451/403).\n"
                    "GitHub Actions ke servers US mein hote hain aur Binance futures US IP block karta hai.\n"
                    "Hal: README dekho — apne PC / non-US free VPS pe `python scanner.py --loop 60` chalao, "
                    "ya BINANCE_BASE secret mein proxy/alternate URL daalo.\n\n" + str(e))
            try:
                if send_mail("⚠️ Binance alert bot: API block ho gayi", body):
                    state["err_mail"] = time.time()
            except Exception as me:
                print("error email bhi nahi gayi:", me)
    except Exception as e:
        print(f"!! scan error: {e}")
    save_state(state)


def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    if "--test-email" in args:
        ok = send_mail("✅ Binance alert bot — test email",
                       "Agar ye email mil gayi to Gmail setup theek hai. Ab alert isi tarah aayenge.")
        sys.exit(0 if ok else 1)
    if "--loop" in args:
        i = args.index("--loop")
        every = int(args[i + 1]) if len(args) > i + 1 and args[i + 1].isdigit() else 60
        while True:
            run_once(dry)
            time.sleep(every)
    run_once(dry)


if __name__ == "__main__":
    main()
