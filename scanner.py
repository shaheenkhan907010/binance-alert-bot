#!/usr/bin/env python3
"""
MEXC Futures — Full Criteria Alert (Gmail)

Alert SIRF tab jab coin ye SAB pass kare:
  1. Resistance (7 din ka high) break, aur 8% se zyada upar nahi
  2. Volume >= 10x (pichle 7 din ke hourly average se)
  3. Breakout se pehle daily RSI(14) 45..82
  4. Breakout se pehle 6 din se uptrend
  5. Funding positive
  6. OI girta nahi  (MEXC OI history nahi deta -> bot khud har ghante snapshot save karta hai)

Usage:
  python scanner.py                 # ek baar scan (GitHub Actions yehi chalata hai)
  python scanner.py --loop 60       # har 60 sec scan (apne PC / VPS pe)
  python scanner.py --test-email    # sirf test email bhejo
  python scanner.py --dry           # email mat bhejo, sirf print karo
"""
import os, sys, json, time, threading, smtplib, ssl
from concurrent.futures import ThreadPoolExecutor
from email.message import EmailMessage

import requests


def _f(name, default):
    v = os.getenv(name)
    return float(v) if v not in (None, "") else float(default)


CFG = {
    "base": (os.getenv("MEXC_BASE") or "https://contract.mexc.com").rstrip("/"),
    "rsi_min": _f("RSI_MIN", 45),
    "rsi_max": _f("RSI_MAX", 82),
    "trend_days": int(_f("TREND_DAYS", 6)),
    "oi_min": _f("OI_MIN", 0),
    "oi_min_age_h": _f("OI_MIN_AGE_H", 3),     # OI compare ke liye kam az kam itne ghante purana snapshot chahiye
    "oi_required": int(_f("OI_REQUIRED", 1)),  # 1 = OI pata na ho to alert nahi; 0 = OI ignore
    "res_days": int(_f("RES_DAYS", 7)),
    "vol_mult": _f("VOL_MULT", 10),
    "max_above": _f("MAX_ABOVE", 8),
    "min_qvol": _f("MIN_QVOL", 1_000_000),
    "cooldown_h": _f("COOLDOWN_H", 6),
}
STATE_FILE = os.getenv("STATE_FILE") or "state.json"
SESSION = requests.Session()
_lock = threading.Lock()
_last = [0.0]


class Blocked(Exception):
    """MEXC ne is IP/location ko block kiya (HTTP 451/403)."""


# ---------------------------------------------------------------- API
def api(path, retries=5):
    """MEXC limit: 20 requests / 2 sec — isliye har request ke beech 0.12s."""
    for i in range(retries):
        with _lock:
            wait = _last[0] + 0.12 - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        try:
            r = SESSION.get(CFG["base"] + path, timeout=25)
        except requests.RequestException:
            if i == retries - 1:
                raise
            time.sleep(2)
            continue
        if r.status_code in (451, 403):
            raise Blocked(f"HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code == 429:
            time.sleep(3)
            continue
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and j.get("success") is False:
            if j.get("code") == 510:            # "requests too frequent"
                time.sleep(2)
                continue
            raise RuntimeError(f"MEXC error {j.get('code')}: {j.get('message')}")
        return j["data"] if isinstance(j, dict) and "data" in j else j
    raise RuntimeError("rate limited")


def get_klines(sym, hours=499):
    """1h klines ko wahi row-format mein badalta hai jo analyze() chahta hai:
    [openTime_ms, open, high, low, close, vol, 0, quote_amount]"""
    now = int(time.time())
    d = api(f"/api/v1/contract/kline/{sym}?interval=Min60&start={now - hours * 3600}&end={now}")
    t = d.get("time") or []
    return [[t[i] * 1000, d["open"][i], d["high"][i], d["low"][i], d["close"][i], d["vol"][i], 0, d["amount"][i]]
            for i in range(len(t))]


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
    return {"sym": sym, "price": price, "res": res, "above": above, "rsi": rsi, "trend": trend,
            "fund": fund, "spike": spike, "oi_chg": None, "oi_hours": None, "c": c}


def pre_ok(r):
    c = r["c"]
    return c["brk"] and c["vol"] and c["rsi"] and c["up"] and c["fund"]


def fmt(p):
    return f"{p:.3f}" if p >= 1 else f"{p:.5f}" if p >= 0.01 else f"{p:.4g}"


# ---------------------------------------------------------------- OI snapshots (MEXC OI history nahi deta)
def update_oi(state, rows, now):
    oi = state.setdefault("oi", {})
    for sym, hold in rows:
        h = oi.setdefault(sym, [])
        if not h or now - h[-1][0] >= 3000:          # ~har ghante ek snapshot
            h.append([now, hold])
        while h and now - h[0][0] > 26 * 3600:
            h.pop(0)
    for sym in [s for s, h in oi.items() if not h or now - h[-1][0] > 26 * 3600]:
        del oi[sym]


def oi_change(state, sym, hold_now, now):
    h = state.get("oi", {}).get(sym) or []
    if not h:
        return None, None
    ts, v = h[0]                                     # sabse purana snapshot (max 24-26h)
    age = (now - ts) / 3600
    if age < CFG["oi_min_age_h"] or not v:
        return None, None
    return (hold_now / v - 1) * 100, age


# ---------------------------------------------------------------- email
def send_mail(subject, body):
    user = os.getenv("GMAIL_USER")
    pw = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    to = os.getenv("ALERT_TO") or user
    if not user or not pw:
        print("!! GMAIL_USER / GMAIL_APP_PASSWORD set nahi hain — email nahi gayi.")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user.strip(), (to or "").strip()
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(user.strip(), pw)
        s.send_message(msg)
    print(f"email bhej di -> {to}: {subject}")
    return True


def alert_body(hits):
    parts = []
    for r in hits:
        s = r["sym"]
        oi = "pata nahi" if r["oi_chg"] is None else f"{r['oi_chg']:+.1f}% ({r['oi_hours']:.0f}h mein)"
        parts.append(
            f"{s}  (MEXC futures)\n"
            f"  Resistance {fmt(r['res'])} toota | Price {fmt(r['price'])} (+{r['above']:.1f}%)\n"
            f"  Volume {r['spike']:.1f}x | RSI(1d, pehle) {r['rsi']:.0f} | {CFG['trend_days']}d trend {r['trend']:+.1f}%\n"
            f"  OI {oi} | Funding {r['fund'] * 100:.4f}%\n"
            f"  MEXC: https://futures.mexc.com/exchange/{s}\n"
            f"  TradingView: https://www.tradingview.com/chart/?symbol=MEXC:{s.replace('_', '')}.P\n"
        )
    return ("Sab criteria pass hue (resistance break + volume + RSI + uptrend + funding + OI).\n"
            "Note: ye MEXC ka data hai — Binance pe chart alag ho sakta hai.\n\n"
            + "\n".join(parts)
            + "\nYe pattern hai, guarantee nahi. Stop-loss zaroor lagao.\n")


# ---------------------------------------------------------------- scan
def scan_once(state, dry=False):
    now = time.time()
    tick = api("/api/v1/contract/ticker")
    liquid = [t for t in tick if str(t.get("symbol", "")).endswith("_USDT")
              and float(t.get("amount24") or 0) >= CFG["min_qvol"]]
    update_oi(state, [(t["symbol"], float(t.get("holdVol") or 0)) for t in liquid], now)
    hold = {t["symbol"]: float(t.get("holdVol") or 0) for t in liquid}
    todo = [(t["symbol"], float(t.get("fundingRate") or 0)) for t in liquid if float(t.get("fundingRate") or 0) > 0]

    def work(item):
        sym, fund = item
        try:
            return analyze(sym, get_klines(sym), fund)
        except Blocked:
            raise
        except Exception as e:  # ek coin fail ho to baaki chalte rahein
            print(f"skip {sym}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        out = [r for r in ex.map(work, todo) if r]

    cand = [r for r in out if pre_ok(r)]
    for r in cand:
        chg, hrs = oi_change(state, r["sym"], hold.get(r["sym"], 0), now)
        r["oi_chg"], r["oi_hours"] = chg, hrs
        r["c"]["oi"] = (chg >= CFG["oi_min"]) if chg is not None else (CFG["oi_required"] == 0)

    hits = sorted([r for r in cand if r["c"]["oi"]], key=lambda r: -r["spike"])
    alerts = state.setdefault("alerts", {})
    fresh = [r for r in hits if now - alerts.get(r["sym"], 0) > CFG["cooldown_h"] * 3600]
    unknown = [r["sym"] for r in cand if r["oi_chg"] is None]
    print(f"{len(liquid)} liquid coins | {len(todo)} funding+ scan hue | {len(cand)} OI se pehle pass | "
          f"{len(hits)} sab pass | {len(fresh)} naye alert" + (f" | OI history abhi nahi: {unknown}" if unknown else ""))
    for r in hits:
        print("  HIT", r["sym"], f"vol {r['spike']:.1f}x above {r['above']:.1f}% rsi {r['rsi']:.0f}")
    if fresh:
        subject = f"🚨 {', '.join(r['sym'] for r in fresh)} — MEXC sab criteria pass"
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
        print(f"!! MEXC ne block kiya: {e}")
        if time.time() - state.get("err_mail", 0) > 24 * 3600 and not dry:
            body = ("MEXC API is server ko block kar rahi hai (HTTP 451/403).\n"
                    "Hal: apne PC pe `python scanner.py --loop 60` chalao ya non-US free VPS use karo.\n\n" + str(e))
            try:
                if send_mail("⚠️ MEXC alert bot: API block ho gayi", body):
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
        ok = send_mail("✅ MEXC alert bot — test email",
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
