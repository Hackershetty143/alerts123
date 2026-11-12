"""
NAVIGATOR v2.2 scanner -> Telegram
-----------------------------------
Every run: pulls 4H + 15m candles from OANDA for each pair, runs the Navigator v2.2 logic on the
LAST CLOSED 15m candle and sends a Telegram message when a setup reaches an alert grade.

Logic ported from Navigator_v2.2.pine (same defaults):
  4H  : LuxAlgo-style dealing range (swing 5, close break, wick extension) -> bias (dir) + premium/discount
        (values of the last CLOSED 4H candle only -> no repaint)
  15m : same range engine (swing 5) -> setup side (BUY = bullish range, SELL = bearish range)
  Checks: Bias (setup side == 4H bias) | LTF Align (BUY in 4H discount / SELL in 4H premium)
          Imbalance (unfilled 15m FVG of the setup side, formed inside the range, in its 50-100% half)
          LTF Sweep (SSL for BUY / BSL for SELL swept on the 100% candle or up to 10 candles before it)
  Grade : bias+align start A+ / A (counter-trend) / B (wrong zone) / C, minus one step per missing FVG / sweep.

Secrets (environment variables): OANDA_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import json, math, os, sys, time
from datetime import datetime, timezone, timedelta
import requests

# ───────────────────────── SETTINGS (same defaults as the indicator) ─────────────────────────
PAIRS = ["XAU_USD", "EUR_USD", "GBP_USD", "USD_JPY", "USD_CAD", "USD_CHF", "NZD_USD",
         "EUR_JPY", "EUR_CAD", "EUR_AUD", "EUR_NZD", "AUD_CAD", "AUD_CHF", "AUD_NZD",
         "GBP_AUD", "GBP_CAD", "GBP_NZD", "NZD_JPY", "NZD_CHF", "NZD_CAD", "CAD_JPY", "CHF_JPY"]
# Which grades send a message. Change without editing code: GitHub repo variable ALERT_GRADES, e.g. "A+,A,B"
ALERT_GRADES = {g.strip().upper() for g in (os.environ.get("ALERT_GRADES") or "A+,A").split(",") if g.strip()}
# Only alert once the last closed 15m candle has closed beyond 50% of the 15m range (into the 50-100% zone):
# BUY -> close at / below the 50% level, SELL -> close at / above it. Repo variable REQUIRE_50 = "0" turns it off.
REQUIRE_50 = (os.environ.get("REQUIRE_50") or "1") != "0"
SWING_MTF = 5                   # 4H range swing length
SWING_LTF = 5                   # 15m range swing length
LIQ_L, LIQ_R = 5, 5             # 15m liquidity swing sensitivity (left / right)
SWEEP_LB = 10                   # sweep window before the range extreme (15m candles)
MAX_FVG = 15                    # 15m FVGs tracked
FIB_LEVEL = 0.71                # used only when FVG_RULE_FIB = True
FVG_RULE_FIB = False            # False = FVG in 50-100% of the range, True = FVG must contain the Fib level
CLOSE_BREAK = True              # BOS / CHoCH confirmed by close
WICK_EXT = True                 # extend the range to new wick extremes on the 0% side
PRICE = "M"                     # OANDA price component: M = mid, B = bid, A = ask
N_LTF, N_MTF = 3000, 1000       # candles requested
OANDA_URL = os.environ.get("OANDA_URL", "https://api-fxpractice.oanda.com")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
IST = timezone(timedelta(hours=5, minutes=30))


# ───────────────────────── ENGINE ─────────────────────────
def pivot_high(h, k, L, R):
    v = h[k]
    for j in range(k - L, k):
        if h[j] >= v: return False
    for j in range(k + 1, k + R + 1):
        if h[j] > v: return False
    return True

def pivot_low(l, k, L, R):
    v = l[k]
    for j in range(k - L, k):
        if l[j] <= v: return False
    for j in range(k + 1, k + R + 1):
        if l[j] < v: return False
    return True


def lux_core(h, l, c, L, close_break=True, extend_wick=True):
    """Bar-by-bar port of luxCore() from the indicator. Returns per-bar lists."""
    n = len(h)
    out = {k: [None] * n for k in ("dir", "hi", "lo", "hiBar", "loBar", "open", "evt")}
    lastPh = lastPl = None; lastPhBar = lastPlBar = None
    maxSincePh = minSincePl = None; maxSincePhBar = minSincePlBar = None
    d = 0; rHi = rLo = None; rHiBar = rLoBar = None; openSide = False
    pull = None; pullBar = None
    for i in range(n):
        k = i - L
        ph = k - L >= 0 and pivot_high(h, k, L, L)
        pl = k - L >= 0 and pivot_low(l, k, L, L)
        if ph:
            lastPh = h[k]; lastPhBar = k; maxSincePh = h[k]; maxSincePhBar = k
        elif maxSincePh is not None and h[i] > maxSincePh:
            maxSincePh = h[i]; maxSincePhBar = i
        if pl:
            lastPl = l[k]; lastPlBar = k; minSincePl = l[k]; minSincePlBar = k
        elif minSincePl is not None and l[i] < minSincePl:
            minSincePl = l[i]; minSincePlBar = i
        evt = 0
        if d == 0 and lastPh is not None and lastPl is not None:
            d = 1 if lastPlBar < lastPhBar else -1
            rHi, rHiBar, rLo, rLoBar, openSide = lastPh, lastPhBar, lastPl, lastPlBar, False
        elif d != 0:
            if openSide:
                if d == 1 and h[i] > rHi:
                    rHi, rHiBar, pull = h[i], i, None
                elif d == -1 and l[i] < rLo:
                    rLo, rLoBar, pull = l[i], i, None
                eq = (rHi + rLo) / 2
                aged = i - (rHiBar if d == 1 else rLoBar) >= L
                atEq = l[i] <= eq if d == 1 else h[i] >= eq
                if aged or atEq:
                    openSide = False
            if d == 1 and i > rHiBar and (pull is None or l[i] < pull):
                pull, pullBar = l[i], i
            if d == -1 and i > rLoBar and (pull is None or h[i] > pull):
                pull, pullBar = h[i], i
            up = c[i] if close_break else h[i]
            dn = c[i] if close_break else l[i]
            if d == 1:
                if dn < rLo:
                    useOrig = not (maxSincePh is None or maxSincePhBar < rLoBar)
                    oH, oHB = (maxSincePh, maxSincePhBar) if useOrig else (rHi, rHiBar)
                    d = -1; rHi, rHiBar = oH, oHB; rLo, rLoBar = l[i], i
                    openSide = True; pull = None; evt = -2
                elif not openSide and up > rHi and pull is not None:
                    rLo, rLoBar = pull, pullBar; rHi, rHiBar = h[i], i
                    openSide = True; pull = None; evt = 1
            else:
                if up > rHi:
                    useOrig = not (minSincePl is None or minSincePlBar < rHiBar)
                    oL, oLB = (minSincePl, minSincePlBar) if useOrig else (rLo, rLoBar)
                    d = 1; rLo, rLoBar = oL, oLB; rHi, rHiBar = h[i], i
                    openSide = True; pull = None; evt = 2
                elif not openSide and dn < rLo and pull is not None:
                    rHi, rHiBar = pull, pullBar; rLo, rLoBar = l[i], i
                    openSide = True; pull = None; evt = -1
            if evt == 0 and extend_wick and not openSide:
                if d == 1 and h[i] > rHi:
                    rHi, rHiBar, pull = h[i], i, None
                elif d == -1 and l[i] < rLo:
                    rLo, rLoBar, pull = l[i], i, None
        out["dir"][i] = d; out["hi"][i] = rHi; out["lo"][i] = rLo
        out["hiBar"][i] = rHiBar; out["loBar"][i] = rLoBar; out["open"][i] = openSide; out["evt"][i] = evt
    return out


def evaluate(ltf, mtf):
    """ltf / mtf: dicts of lists t (epoch s, candle open), o, h, l, c. Returns the state of the LAST ltf candle."""
    h, l, c, t = ltf["h"], ltf["l"], ltf["c"], ltf["t"]
    n = len(c)
    # ---- 4H: value of the last CLOSED 4H candle at each 15m candle ----
    M = lux_core(mtf["h"], mtf["l"], mtf["c"], SWING_MTF, CLOSE_BREAK, WICK_EXT)
    mt = mtf["t"]
    # ---- 15m ----
    Lx = lux_core(h, l, c, SWING_LTF, CLOSE_BREAK, WICK_EXT)
    # liquidity (liquiditySwingCalc) -> sweep bars
    ssl_bars, bsl_bars = [], []
    bsl = ssl = None
    for i in range(n):
        k = i - LIQ_R
        if k - LIQ_L >= 0:
            if pivot_high(h, k, LIQ_L, LIQ_R): bsl = h[k]
            if pivot_low(l, k, LIQ_L, LIQ_R): ssl = l[k]
        if bsl is not None and h[i] >= bsl:
            bsl_bars.append(i); bsl = None
        if ssl is not None and l[i] <= ssl:
            ssl_bars.append(i); ssl = None

    def swept(bars, ext):
        return any(ext - SWEEP_LB <= b <= ext for b in bars)

    fvgs = []          # [top, bottom, bullish, createdBar]
    range_sweep = False
    st = None
    j = -1
    for i in range(n):
        # BOS / CHoCH bar -> decide the sweep check for this range
        if Lx["evt"][i] != 0:
            ext = Lx["loBar"][i] if Lx["dir"][i] == 1 else Lx["hiBar"][i]
            range_sweep = swept(ssl_bars if Lx["dir"][i] == 1 else bsl_bars, ext)
        # FVG creation (confirmed candle) + full-fill removal
        if i > 2:
            if l[i] > h[i - 2]:
                fvgs.append([l[i], h[i - 2], True, i - 1])
            if h[i] < l[i - 2]:
                fvgs.append([l[i - 2], h[i], False, i - 1])
            while len(fvgs) > MAX_FVG: fvgs.pop(0)
        fvgs = [z for z in fvgs if not (l[i] <= z[1] if z[2] else h[i] >= z[0])]
        if i < n - 1:
            continue
        # ---- evaluate the last candle ----
        while j + 1 < len(mt) and mt[j + 1] + 4 * 3600 <= t[i]:
            j += 1  # last 4H candle closed before this 15m candle opened (same as the indicator)
        hDir = M["dir"][j] if j >= 0 else 0
        hHi = M["hi"][j] if j >= 0 else None
        hLo = M["lo"][j] if j >= 0 else None
        hReady = hDir != 0 and hHi is not None and hLo is not None and hHi > hLo
        lDir, lHi, lLo = Lx["dir"][i], Lx["hi"][i], Lx["lo"][i]
        lReady = lDir != 0 and lHi is not None and lLo is not None and lHi > lLo
        if not (hReady and lReady):
            return None
        hEq = (hHi + hLo) / 2
        hPos = 100 * (c[i] - hLo) / (hHi - hLo)
        hPD = 1 if c[i] > hEq else -1 if c[i] < hEq else 0
        lEq = (lHi + lLo) / 2
        left = min(Lx["hiBar"][i], Lx["loBar"][i])
        fibY = lHi - (lHi - lLo) * FIB_LEVEL if lDir == 1 else lLo + (lHi - lLo) * FIB_LEVEL
        fvg_ok = False
        for top, bot, bull, cb in fvgs:
            if bull == (lDir == 1) and cb >= left:
                in_half = (bot <= lEq and top >= lLo) if lDir == 1 else (top >= lEq and bot <= lHi)
                on_fib = bot <= fibY <= top
                if (on_fib if FVG_RULE_FIB else in_half):
                    fvg_ok = True
        with_bias = lDir == hDir
        align = (lDir == 1 and hPD == -1) or (lDir == -1 and hPD == 1)
        base = 0 if (with_bias and align) else 1 if align else 2 if with_bias else 3
        step = base + (0 if fvg_ok else 1) + (0 if range_sweep else 1)
        grade = ["A+", "A", "B", "C"][step] if step <= 3 else "F"
        in50 = c[i] <= lEq if lDir == 1 else c[i] >= lEq
        st = dict(side="BUY" if lDir == 1 else "SELL", in50=in50, grade=grade, bias="BULLISH" if hDir == 1 else "BEARISH",
                  zone="Premium" if hPD == 1 else "Discount" if hPD == -1 else "EQ", pos=hPos,
                  with_bias=with_bias, align=align, fvg=fvg_ok, sweep=range_sweep,
                  r_hi=lHi, r_lo=lLo, fib=fibY, eq=lEq, origin_t=t[left], close=c[i], bar_t=t[i])
    return st


# ───────────────────────── DATA / TELEGRAM ─────────────────────────
def fetch(pair, gran, count, token):
    r = requests.get(f"{OANDA_URL}/v3/instruments/{pair}/candles",
                     headers={"Authorization": f"Bearer {token}"},
                     params={"granularity": gran, "count": count, "price": PRICE,
                             "dailyAlignment": 17, "alignmentTimezone": "America/New_York"},
                     timeout=30)
    r.raise_for_status()
    key = {"M": "mid", "B": "bid", "A": "ask"}[PRICE]
    out = {"t": [], "o": [], "h": [], "l": [], "c": []}
    for cd in r.json()["candles"]:
        if not cd["complete"]:
            continue  # only closed candles
        p = cd[key]
        ts = cd["time"].split(".")[0].rstrip("Z")
        out["t"].append(int(datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()))
        for a, b in (("o", "o"), ("h", "h"), ("l", "l"), ("c", "c")):
            out[a].append(float(p[b]))
    return out


def fmt(x, pair):
    return f"{x:.3f}" if ("JPY" in pair or "XAU" in pair) else f"{x:.5f}"


def message(pair, s):
    tick = lambda b: "✅" if b else "❌"
    sym = pair.replace("_", "")
    icon = "🟢" if s["side"] == "BUY" else "🔴"
    tag = "  (counter-trend)" if not s["with_bias"] else ""
    link = f"https://www.tradingview.com/chart/?symbol=OANDA:{sym}&amp;interval=15"
    return (f"{icon} <b>{sym}</b>  •  <b>{s['side']} ({s['grade']})</b>{tag}\n\n"
            f"Bias: {s['bias']} (4H)\n"
            f"Range: {s['zone']} {s['pos']:.0f}%\n"
            f"{tick(s['align'])} LTF Align\n"
            f"{tick(s['fvg'])} Imbalance ({'Bull' if s['side'] == 'BUY' else 'Bear'} FVG)\n"
            f"{tick(s['sweep'])} LTF Sweep ({'SSL' if s['side'] == 'BUY' else 'BSL'})\n\n"
            f'<a href="{link}">📈 Open {sym} on TradingView</a>')


def send_telegram(text, bot, chat):
    r = requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
                      timeout=30)
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:200])


def main():
    token = os.environ["OANDA_TOKEN"]
    bot = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHAT_ID"]
    try:
        state = json.load(open(STATE_FILE))
    except Exception:
        state = {}
    if os.environ.get("TEST_MESSAGE") == "1":
        send_telegram("🧭 NAVIGATOR scanner is connected. You'll get a message here when a setup forms.", bot, chat)
    print("alert grades:", sorted(ALERT_GRADES), "| require close past 50%:", REQUIRE_50)
    sent = 0
    for pair in PAIRS:
        try:
            ltf = fetch(pair, "M15", N_LTF, token)
            mtf = fetch(pair, "H4", N_MTF, token)
            s = evaluate(ltf, mtf)
        except Exception as e:
            print(pair, "error:", e)
            continue
        if not s:
            print(pair, "not ready"); continue
        print(f"{pair}: {s['side']} ({s['grade']})  bias={s['bias']} zone={s['zone']} fvg={s['fvg']} sweep={s['sweep']} past50={s['in50']}")
        # one message per setup: same range origin + side + grade is not sent twice
        key = f"{s['side']}|{s['origin_t']}|{s['grade']}"
        if s["grade"] in ALERT_GRADES and (s["in50"] or not REQUIRE_50) and state.get(pair) != key:
            send_telegram(message(pair, s), bot, chat)
            sent += 1
            state[pair] = key
        time.sleep(0.2)
    json.dump(state, open(STATE_FILE, "w"), indent=1, sort_keys=True)
    print("messages sent:", sent)


if __name__ == "__main__":
    main()
