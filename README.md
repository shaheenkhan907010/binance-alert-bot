# Binance Futures — Full Criteria Alert (Gmail, free)

Har 5 minute mein Binance ke saare USDT futures coins scan hote hain. **Sirf tab email aati hai** jab coin ye sab pass kare:

1. Resistance (7 din ka high) break, aur 8% se zyada upar nahi
2. Volume >= 10x
3. Breakout se pehle daily RSI 45–82
4. Breakout se pehle 6 din se uptrend
5. Funding positive
6. OI 24h girta nahi

Same coin ki email 6 ghante tak dobara nahi aati.

## Setup (10 minute)

**1. Gmail App Password banao**
- Google Account → Security → **2-Step Verification** ON karo
- Phir search karo **"App passwords"** → naam likho `binance-bot` → 16 letters ka password copy karo

**2. GitHub repo banao**
- github.com → New repository → naam `binance-alert-bot` → **Public** chuno → Create
- Is folder ki saari files upload karo (Add file → Upload files). `.github/workflows/scan.yml` bhi jana zaroori hai
  (Mac pe hidden folder dikhane ke liye Cmd+Shift+. dabao)

**3. Secrets daalo**
Repo → Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `GMAIL_USER` | tumhara gmail address |
| `GMAIL_APP_PASSWORD` | 16 letters wala app password |
| `ALERT_TO` | (optional) jis email pe alert chahiye, warna wahi gmail |

**4. Test karo**
Actions tab → **scan** → Run workflow → **test_email** tick karo → Run. 1 minute mein Gmail mein test email aani chahiye (spam folder bhi dekho).

Uske baad bot khud har 5 minute chalta rahega.

## Zaroori limits (sach)

- **Binance US IP block karta hai.** GitHub Actions ke servers US mein hote hain, isliye run log mein `HTTP 451` aa sakta hai. Aisa ho to bot tumhe ek email bhejega ("API block ho gayi"). Us case mein:
  - apne PC pe chalao: `pip install requests` phir `GMAIL_USER=... GMAIL_APP_PASSWORD=... python scanner.py --loop 60`
  - ya kisi non-US free VPS (jaise Oracle Cloud Free) pe wahi command chalao
- **GitHub schedule late ho sakta hai** — kabhi 5 ki jagah 10–20 minute. Tez pump mein alert late aa sakta hai. Har-minute alert ke liye PC/VPS pe `--loop 60` behtar hai.
- **Public repo = free unlimited.** Private repo mein 2000 free minutes/month hain, to `scan.yml` mein cron `*/30 * * * *` kar do.
- Public repo mein 60 din activity na ho to GitHub schedule band kar deta hai — Actions tab mein dobara enable kar do.
- Ye pattern hai, guarantee nahi. Entry se pehle chart dekho aur stop-loss lagao.

## Settings badalni hon
`scan.yml` ke `env:` mein ye add kar sakte ho: `VOL_MULT` (10), `MAX_ABOVE` (8), `RSI_MIN` (45), `RSI_MAX` (82), `TREND_DAYS` (6), `OI_MIN` (0), `RES_DAYS` (7), `MIN_QVOL` (1000000), `COOLDOWN_H` (6).
Alert kam aayen to `VOL_MULT` 10 se 5 kar do.
