#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_trader.py (multi-asset CFD simulace) - papírový obchodník. ŽÁDNÉ SKUTEČNÉ PENÍZE.

Simuluje obchodování CFD na akciích, ETF (indexy, zlato) a kryptoměnách: LONG i SHORT,
spread v USD na kus, noční poplatek (22:00 GMT, jiná sazba pro Long a Short), FX poplatek,
close-out při velké ztrátě (jako u EU brokerů). Parametry podle údajů z Trading 212 CFD.
Každé spuštění udělá JEDNO kolo (plánovač ho pouští každých ~10 minut).

Ceny: US akcie a ETF z Finnhubu (potřebuje zdarma klíč FINNHUB_API_KEY),
      krypto z Coinbase (bez klíče). US trh se obchoduje jen v řádné seanci.

Režimy:
  python3 paper_trader.py --once --notify   jedno kolo + (jednou za hodinu) přehled na Telegram
  python3 paper_trader.py --once            jen jedno kolo
  python3 paper_trader.py --check           zkontroluje zdroje dat (ceny a jejich stáří)
  python3 paper_trader.py --telegram        pošle podrobný ranní report na Telegram
  python3 paper_trader.py --telegram --short   pošle krátký hodinový přehled
  python3 paper_trader.py --report [--short]   vypíše report do okna (nic neposílá)
  python3 paper_trader.py --demo            rychlý test na vymyšlených cenách (data_cfd/demo_*)
Přepínač --dry: u --telegram a --notify jen vypíše text a neodešle ho.

Používá jen standardní knihovny Pythonu, nic se neinstaluje.
"""

import csv
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

# ========================= NASTAVENÍ =========================
# (symbol, zdroj, třída, popisek, spread v USD na 1 ks, noční sazba Long v % za den, noční sazba Short v % za den)
# zdroj "us" = US akcie/ETF přes Finnhub, "crypto" = Coinbase. Další nástroj = další řádek.
# Spready a noční sazby jsou hodnoty podle údajů z Trading 212 CFD (spread je průměr,
# skutečný spread kolísá). Záporná sazba = poplatek, který platíš.
INSTRUMENTS = [
    ("SPY",  "us",     "index",  "S&P 500 (ETF)",    0.04,  -0.0251, -0.0694),
    ("QQQ",  "us",     "index",  "Nasdaq 100 (ETF)", 0.05,  -0.0206, -0.0259),
    ("GLD",  "us",     "gold",   "Zlato (ETF GLD)",  0.06,  -0.0206, -0.0259),
    ("AAPL", "us",     "stock",  "Apple",            0.14,  -0.0251, -0.0694),
    ("MSFT", "us",     "stock",  "Microsoft",        0.47,  -0.0251, -0.0694),
    ("NVDA", "us",     "stock",  "Nvidia",           0.07,  -0.0251, -0.0694),
    ("TSLA", "us",     "stock",  "Tesla",            0.20,  -0.0251, -0.0694),
    ("AMD",  "us",     "stock",  "AMD",              0.72,  -0.0251, -0.0694),
    ("AVGO", "us",     "stock",  "Broadcom",         0.18,  -0.0251, -0.0694),
    ("ASML", "us",     "stock",  "ASML (ADR v USD)", 1.40,  -0.0251, -0.0694),
    ("META", "us",     "stock",  "Meta",             0.68,  -0.0251, -0.0694),
    ("NFLX", "us",     "stock",  "Netflix",          0.07,  -0.0251, -0.0694),
    ("BTC",  "crypto", "crypto", "Bitcoin",         58.39,  -0.0113, -0.0721),
    ("ETH",  "crypto", "crypto", "Ethereum",         0.63,  -0.0113, -0.0721),
]

# Parametry podle třídy. Páka odpovídá limitům ESMA pro EU retail klienty.
CLASS = {
    #           páka  stop-loss  pásmo signálu
    "index":  {"lev": 20, "stop": 0.015, "band": 0.0030},
    "gold":   {"lev": 20, "stop": 0.015, "band": 0.0030},
    "stock":  {"lev": 5,  "stop": 0.030, "band": 0.0080},
    "crypto": {"lev": 2,  "stop": 0.040, "band": 0.0080},
}

# Noční poplatek (overnight): strhává se denně v 22:00 GMT (UTC) z hodnoty pozice
# (počet kusů x cena x denní sazba). Násobek podle dne v týdnu (Po=0 ... Ne=6):
# víkend (3 dny) se strhává v neděli ve 22:00 GMT, v pátek a v sobotu se nestrhává nic.
ROLLOVER_UTC_HOUR = 22
ROLLOVER_MULT = {0: 1, 1: 1, 2: 1, 3: 1, 4: 0, 5: 0, 6: 3}

# FX poplatek 0,5 % z realizovaného zisku nebo ztráty při zavření obchodu,
# pokud účet u Trading 212 není v USD. Je-li tvůj účet v USD, nastav True.
ACCOUNT_IN_USD = False
FX_FEE_PCT = 0.005

START_CASH = 10000.0         # virtuální účet v USD
NOTIONAL_FRACTION = 0.10     # velikost jedné pozice = 10 % startovního účtu (hodnota, ne marže)
FAST_N = 6                   # krátký klouzavý průměr (počet kol; při 10 min = 1 hodina)
SLOW_N = 24                  # dlouhý klouzavý průměr (počet kol; při 10 min = 4 hodiny)
CLOSEOUT_LEVEL = 0.5         # při poklesu účtu na 50 % použité marže se pozice zavírá (ESMA)
MAX_STALE_MIN = 30           # cena starší než 30 minut = nepoužije se (svátek, přerušení)
POLL_MIN = 10                # jak často se kolo spouští (jen pro výpočty a texty)
REPORT_TZ = "Europe/Prague"  # časová zóna, ve které se píší časy v reportu
DEMO_TICKS = 3000            # kolik kol (po 10 minutách) proběhne v režimu --demo
# =============================================================

SYMS = [i[0] for i in INSTRUMENTS]
INFO = {}
for _sym, _src, _cls, _label, _spread, _fl, _fs in INSTRUMENTS:
    INFO[_sym] = dict(CLASS[_cls], src=_src, cls=_cls, label=_label, spread=_spread,
                      fin_long=_fl / 100.0, fin_short=_fs / 100.0)
FMT = 2                  # verze formátu dat (kvůli přechodu ze starší verze)
TICKS_PER_DAY = int(24 * 60 / POLL_MIN)

DEMO = "--demo" in sys.argv
DRY = "--dry" in sys.argv
PREFIX = "demo_" if DEMO else ""
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data_cfd")
F_STATE = os.path.join(DATA_DIR, PREFIX + "state.json")
F_TRADES = os.path.join(DATA_DIR, PREFIX + "trades.csv")
F_EQUITY = os.path.join(DATA_DIR, PREFIX + "equity.csv")

_clock = [None]          # v demu "falešné hodiny", jinak se bere skutečný čas


def now_dt():
    return _clock[0] or datetime.now(timezone.utc)


def now():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")      # vždy UTC


def parse_ts(t):
    return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def local(dt):
    try:
        return dt.astimezone(ZoneInfo(REPORT_TZ))
    except Exception:
        return dt


def money(x):
    return "{:,.2f}".format(x).replace(",", " ")


def pct(a, b):
    return (a / b - 1) * 100


def us_session_open(dt):
    """Řádná seance USA: po-pá 9:30-16:00 newyorského času (svátky zachytí kontrola stáří ceny)."""
    try:
        ny = dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        ny = dt - timedelta(hours=5)
    if ny.weekday() >= 5:
        return False
    mins = ny.hour * 60 + ny.minute
    return 9 * 60 + 30 <= mins < 16 * 60


# ---------------------------- ceny ----------------------------
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trader/4.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def short_err(e):
    code = getattr(e, "code", None)
    return "HTTP %s" % code if code else type(e).__name__


def finnhub_quote(sym):
    """Vrací (cena, stáří v sekundách). Potřebuje FINNHUB_API_KEY."""
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        raise RuntimeError("chybí FINNHUB_API_KEY (Settings > Secrets > Actions)")
    try:
        d = fetch_json("https://finnhub.io/api/v1/quote?symbol=%s&token=%s" % (sym, key))
    except Exception as e:
        raise RuntimeError("Finnhub %s: %s" % (sym, short_err(e)))
    c, t = d.get("c"), d.get("t")
    if not c or not t:
        raise RuntimeError("Finnhub %s: prázdná odpověď" % sym)
    return float(c), max(0.0, now_dt().timestamp() - float(t))


def crypto_quote(sym):
    """Cena z Coinbase, záloha Binance. Vrací (cena, 0)."""
    err = ""
    try:
        d = fetch_json("https://api.coinbase.com/v2/prices/%s-USD/spot" % sym)
        return float(d["data"]["amount"]), 0.0
    except Exception as e:
        err = "Coinbase %s" % short_err(e)
    try:
        d = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=%sUSDT" % sym)
        return float(d["price"]), 0.0
    except Exception as e:
        raise RuntimeError("%s, Binance %s" % (err, short_err(e)))


def real_quote(sym):
    return finnhub_quote(sym) if INFO[sym]["src"] == "us" else crypto_quote(sym)


_fake = {}
FAKE_START = {"SPY": 600, "QQQ": 520, "GLD": 290, "AAPL": 230, "MSFT": 450, "NVDA": 140,
              "TSLA": 350, "AMD": 150, "AVGO": 200, "ASML": 700, "META": 600, "NFLX": 110,
              "BTC": 90000, "ETH": 3200}
FAKE_VOL = {"index": 0.0008, "gold": 0.0008, "stock": 0.0015, "crypto": 0.0025}


def fake_quote(sym):
    """Vymyšlená cena (náhodná procházka) pro režim --demo."""
    p = _fake.get(sym, float(FAKE_START.get(sym, 100)))
    p *= 1 + random.gauss(0, FAKE_VOL[INFO[sym]["cls"]])
    _fake[sym] = p
    return p, 20.0


# ---------------------------- stav ----------------------------
def new_state():
    return {"fmt": FMT, "cash": START_CASH, "pos": {}, "start_prices": {}, "hist": {}, "trend": {},
            "n_ticks": 0, "started": now(), "last_report": "",
            "spread_paid": 0.0, "financing_paid": 0.0, "fx_paid": 0.0,
            "issues": {}, "us_age": None}


def load_state():
    if os.path.exists(F_STATE):
        with open(F_STATE) as f:
            s = json.load(f)
        if s.get("fmt") != FMT:
            # data ze starší verze skriptu: přejmenujeme je (.v1) a začneme znovu čistě
            for p in (F_STATE, F_TRADES, F_EQUITY):
                if os.path.exists(p):
                    os.replace(p, p + ".v1")
            return new_state()
        for k, v in new_state().items():
            s.setdefault(k, v)
        return s
    return new_state()


def save_state(s):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = F_STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, F_STATE)


def append_csv(path, header, row):
    os.makedirs(DATA_DIR, exist_ok=True)
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if is_new:
            w.writerow(header)
        w.writerow(row)


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def current_prices(s):
    return {n: h[-1][1] for n, h in s["hist"].items() if h and n in INFO}


# ------------------------- CFD matematika -------------------------
def pos_pnl(p, price):
    """Nerealizovaný zisk/ztráta, kdyby se pozice zavřela hned (včetně spreadu)."""
    half = p["spread"] / 2             # spread je v USD na 1 ks
    if p["side"] == "LONG":
        return p["units"] * (price - half - p["entry"])
    return p["units"] * (p["entry"] - (price + half))


def equity(s, prices):
    return s["cash"] + sum(pos_pnl(p, prices[n]) for n, p in s["pos"].items() if n in prices)


def used_margin(s):
    return sum(p["margin"] for p in s["pos"].values())


def buyhold(s, prices):
    """Srovnání: stejný díl každého nástroje, bez páky, drženo od začátku."""
    r = [prices[n] / s["start_prices"][n] for n in SYMS if n in prices and n in s["start_prices"]]
    return START_CASH * sum(r) / len(r) if r else START_CASH


def spy_hold(s, prices):
    if "SPY" in prices and "SPY" in s["start_prices"]:
        return START_CASH * prices["SPY"] / s["start_prices"]["SPY"]
    return START_CASH


# --------------------------- obchodování ---------------------------
TRADE_HEADER = ["time", "symbol", "action", "side", "price", "notional_usd", "pnl_usd", "reason"]
EQUITY_HEADER = ["time", "equity_usd", "buyhold_usd", "spy_usd"]


def open_pos(s, sym, side, price, reason, prices):
    info = INFO[sym]
    notional = START_CASH * NOTIONAL_FRACTION
    margin = notional / info["lev"]
    if equity(s, prices) - used_margin(s) < margin:
        return False        # broker by pozici neotevřel (málo volné marže)
    half = info["spread"] / 2
    entry = price + half if side == "LONG" else price - half
    units = notional / entry
    s["pos"][sym] = {"side": side, "units": units, "entry": entry, "mid_entry": price,
                     "margin": margin, "notional": notional, "spread": info["spread"],
                     "fin_rate": info["fin_long"] if side == "LONG" else info["fin_short"],
                     "opened": now(), "fin_ts": now_dt().timestamp(), "fin_paid": 0.0}
    s["spread_paid"] += units * half
    append_csv(F_TRADES, TRADE_HEADER,
               [now(), sym, "OPEN", side, round(entry, 4), round(notional, 2), "", reason])
    return True


def close_pos(s, sym, price, reason):
    p = s["pos"].pop(sym)
    half = p["spread"] / 2
    if p["side"] == "LONG":
        exit_px = price - half
        gross = p["units"] * (exit_px - p["entry"])
    else:
        exit_px = price + half
        gross = p["units"] * (p["entry"] - exit_px)
    fx = 0.0 if ACCOUNT_IN_USD else abs(gross) * FX_FEE_PCT      # FX poplatek z realizovaného výsledku
    s["cash"] += gross - fx
    s["spread_paid"] += p["units"] * half
    s["fx_paid"] += fx
    net = gross - fx - p["fin_paid"]    # noční poplatky už byly strženy průběžně
    append_csv(F_TRADES, TRADE_HEADER,
               [now(), sym, "CLOSE", p["side"], round(exit_px, 4), round(p["notional"], 2),
                round(net, 4), reason])


def rollovers_between(t0, t1):
    """Okamžiky strhávání nočního poplatku (denně v 22:00 UTC) v intervalu (t0, t1]."""
    out = []
    d = t0.replace(hour=ROLLOVER_UTC_HOUR, minute=0, second=0, microsecond=0)
    if d <= t0:
        d += timedelta(days=1)
    while d <= t1:
        out.append(d)
        d += timedelta(days=1)
    return out


def accrue_financing(s, prices, ts):
    """Strhne noční poplatek za každé 22:00 UTC, které pozice 'přečkala' od posledního kola.
    Výše = počet kusů x cena x denní sazba (podle Long/Short) x násobek dne (neděle = 3 dny)."""
    t1 = datetime.fromtimestamp(ts, timezone.utc)
    for n, p in s["pos"].items():
        t0 = datetime.fromtimestamp(p["fin_ts"], timezone.utc)
        mult = sum(ROLLOVER_MULT[d.weekday()] for d in rollovers_between(t0, t1))
        p["fin_ts"] = ts
        if mult == 0 or n not in prices:
            continue
        change = p["units"] * prices[n] * p["fin_rate"] * mult      # záporná sazba = poplatek
        s["cash"] += change
        p["fin_paid"] += -change
        s["financing_paid"] += -change


def check_closeout(s, prices):
    """Pravidlo ESMA: když účet klesne na 50 % použité marže, zavírá se nejhorší pozice."""
    while s["pos"]:
        if equity(s, prices) > CLOSEOUT_LEVEL * used_margin(s):
            return
        worst = min(s["pos"], key=lambda n: pos_pnl(s["pos"][n], prices.get(n, s["pos"][n]["mid_entry"])))
        close_pos(s, worst, prices.get(worst, s["pos"][worst]["mid_entry"]), "close-out (margin call)")
    if s["cash"] < 0:
        s["cash"] = 0.0     # ochrana před záporným zůstatkem


def trade_logic(s, sym, h, prices):
    """TADY JE STRATEGIE. Později můžeš nahradit jiným pravidlem."""
    info = INFO[sym]
    price = h[-1][1]
    pos = s["pos"].get(sym)

    if pos:  # stop-loss proti směru pozice
        if pos["side"] == "LONG" and price <= pos["mid_entry"] * (1 - info["stop"]):
            close_pos(s, sym, price, "stop-loss")
            return
        if pos["side"] == "SHORT" and price >= pos["mid_entry"] * (1 + info["stop"]):
            close_pos(s, sym, price, "stop-loss")
            return

    if len(h) < SLOW_N + 1:
        return  # ještě nemáme dost historie
    fast = sum(pt[1] for pt in h[-FAST_N:]) / FAST_N
    slow = sum(pt[1] for pt in h[-SLOW_N:]) / SLOW_N

    old_trend = s["trend"].get(sym)
    if fast > slow * (1 + info["band"]):
        trend = "UP"
    elif fast < slow * (1 - info["band"]):
        trend = "DOWN"
    else:
        trend = old_trend              # uvnitř pásma se nic nemění
    s["trend"][sym] = trend
    if old_trend is None or trend is None or trend == old_trend:
        return                         # první určení trendu jen zapíšeme, neobchodujeme

    if trend == "UP":                  # začátek růstu -> LONG
        if pos and pos["side"] == "SHORT":
            close_pos(s, sym, price, "obrat nahoru")
            pos = None
        if not pos:
            open_pos(s, sym, "LONG", price, "trend nahoru", prices)
    else:                              # začátek poklesu -> SHORT
        if pos and pos["side"] == "LONG":
            close_pos(s, sym, price, "obrat dolu")
            pos = None
        if not pos:
            open_pos(s, sym, "SHORT", price, "trend dolu", prices)


def fetch_all(s, quote_fn, ts):
    """Stáhne ceny všech nástrojů. Vrací (slovník sym -> čerstvá cena, statistiky zdrojů)."""
    got = {}
    us_open = us_session_open(now_dt())
    stats = {"us": {"ok": 0, "fail": 0, "stale": 0, "err": "", "age": 0.0},
             "crypto": {"ok": 0, "fail": 0, "stale": 0, "err": "", "age": 0.0}}
    for sym in SYMS:
        src = INFO[sym]["src"]
        st = stats[src]
        if src == "us" and not us_open:
            continue
        try:
            price, age = quote_fn(sym)
        except Exception as e:
            st["fail"] += 1
            st["err"] = str(e)[:100]
            continue
        if age > MAX_STALE_MIN * 60:
            st["stale"] += 1
            continue
        st["ok"] += 1
        st["age"] = max(st["age"], age)
        h = s["hist"].setdefault(sym, [])
        h.append([ts, price])
        del h[:-(SLOW_N + 5)]
        s["start_prices"].setdefault(sym, price)
        got[sym] = price
        if not DEMO and src == "us":
            time.sleep(0.1)             # ohleduplnost k API
    for src, st in stats.items():
        tried = st["ok"] + st["fail"] + st["stale"]
        if tried == 0:
            continue
        iss = s["issues"].setdefault(src, {"fails": 0, "last": ""})
        if st["ok"] > 0:
            iss["fails"], iss["last"] = 0, ""
        else:
            iss["fails"] += 1
            iss["last"] = st["err"] or "žádná čerstvá data (svátek nebo zpožděný zdroj?)"
    s["us_age"] = stats["us"]["age"] if stats["us"]["ok"] else None
    return got, stats


def tick(s, quote_fn, show=True):
    """Jedno kolo. Vrací True, když se podařilo stáhnout aspoň jednu cenu."""
    s["n_ticks"] += 1
    ts = now_dt().timestamp()
    got, stats = fetch_all(s, quote_fn, ts)
    if not got:
        s["n_ticks"] -= 1
        if show:
            for src, st in stats.items():
                if st["err"]:
                    print("  %s: %s" % (src, st["err"]))
        return False

    prices = current_prices(s)
    accrue_financing(s, prices, ts)
    check_closeout(s, prices)
    for sym in got:
        trade_logic(s, sym, s["hist"][sym], prices)

    eq, bh, spy = equity(s, prices), buyhold(s, prices), spy_hold(s, prices)
    append_csv(F_EQUITY, EQUITY_HEADER, [now(), round(eq, 2), round(bh, 2), round(spy, 2)])
    if show:
        us, cr = stats["us"], stats["crypto"]
        us_txt = ("USA %d/%d (stáří %d min)" % (us["ok"], us["ok"] + us["fail"] + us["stale"], us["age"] / 60)
                  if us["ok"] + us["fail"] + us["stale"] else "USA zavřeno")
        print("%s UTC | %s, krypto %d/%d | účet %s (%+.2f %%) | pozic %d" % (
            now(), us_txt, cr["ok"], cr["ok"] + cr["fail"] + cr["stale"],
            money(eq), pct(eq, START_CASH), len(s["pos"])))
        for src, st in stats.items():
            if st["fail"] or st["stale"]:
                print("  POZOR %s: selhalo %d, stará data %d. %s" % (src, st["fail"], st["stale"], st["err"]))
    return True


# ----------------------------- reporty -----------------------------
def eq_rows():
    out = []
    for r in read_csv(F_EQUITY):
        out.append((parse_ts(r["time"]), float(r["equity_usd"]), float(r["buyhold_usd"]),
                    float(r.get("spy_usd") or r["buyhold_usd"])))
    return out


def row_at_or_before(rows, t):
    for r in reversed(rows):
        if r[0] <= t:
            return r
    return rows[0] if rows else None


def count_rows_since(rows, t):
    n = 0
    for r in reversed(rows):
        if r[0] > t:
            n += 1
        else:
            break
    return n


def sparkline(vals):
    blocks = "▁▂▃▄▅▆▇█"
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return blocks[0] * len(vals)
    return "".join(blocks[int((v - lo) / (hi - lo) * 7 + 0.5)] for v in vals)


def spark_series(rows, t_from, n):
    vals = [r[1] for r in rows if r[0] >= t_from]
    if len(vals) > n:
        step = (len(vals) - 1) / float(n - 1)
        vals = [vals[int(round(i * step))] for i in range(n)]
    return vals


def max_drawdown(rows):
    peak, worst = None, 0.0
    for r in rows:
        peak = r[1] if peak is None else max(peak, r[1])
        worst = min(worst, (r[1] / peak - 1) * 100)
    return worst


def pos_line(s, sym, price):
    p = s["pos"][sym]
    return "%s %s (hodnota %s USD, nerealizovaně %+.2f USD)" % (
        sym, p["side"], money(p["units"] * price), pos_pnl(p, price))


def trade_text(r):
    if r["action"] == "OPEN":
        return "OTEVŘENO %s %s @ %s (%s)" % (r["side"], r["symbol"], money(float(r["price"])), r["reason"])
    return "ZAVŘENO %s %s @ %s, výsledek %+.2f USD (%s)" % (
        r["side"], r["symbol"], money(float(r["price"])), float(r["pnl_usd"]), r["reason"])


def data_health_lines(s):
    out = []
    names = {"us": "USA (Finnhub)", "crypto": "Krypto (Coinbase)"}
    for src, iss in s["issues"].items():
        if iss["fails"] > 0:
            out.append("PROBLÉM s daty - %s: %d kol po sobě bez čerstvé ceny. %s" % (
                names.get(src, src), iss["fails"], iss["last"]))
    age = s.get("us_age")
    if age is not None and age > 300:
        out.append("Pozor: ceny z USA jsou staré %d min, free zdroj asi není v reálném čase." % (age / 60))
    return out


def build_short_report():
    """Krátký přehled (posílá se jednou za hodinu)."""
    if not os.path.exists(F_STATE):
        return "Zatím nejsou žádná data."
    s = load_state()
    prices = current_prices(s)
    if not prices:
        return "Zatím se nepodařilo stáhnout žádnou cenu."
    eq, bh, spy = equity(s, prices), buyhold(s, prices), spy_hold(s, prices)
    rows = eq_rows()
    t_now = now_dt()
    since = t_now - timedelta(minutes=60)
    base = row_at_or_before(rows, since)
    hour_txt = ", za poslední hodinu %+.2f %%" % pct(eq, base[1]) if base else ""
    when = local(rows[-1][0]) if rows else local(t_now)

    trades = read_csv(F_TRADES)
    new = [r for r in trades if parse_ts(r["time"]) > since]

    out = []
    out.append("PAPER TRADER (CFD) - hodinový přehled")
    out.append(when.strftime("%d.%m.%Y %H:%M") + " (" + REPORT_TZ + ")")
    out.append("Účet: %s USD (%+.2f %% od startu%s)" % (money(eq), pct(eq, START_CASH), hour_txt))
    out.append("Kup a drž všech %d nástrojů: %+.2f %%, SPY: %+.2f %% (od startu)" % (
        len(SYMS), pct(bh, START_CASH), pct(spy, START_CASH)))
    open_now = sorted(s["pos"], key=lambda n: -abs(pos_pnl(s["pos"][n], prices.get(n, s["pos"][n]["mid_entry"]))))
    if open_now:
        out.append("Otevřené pozice (%d):" % len(open_now))
        for n in open_now[:6]:
            out.append(" - " + pos_line(s, n, prices.get(n, s["pos"][n]["mid_entry"])))
        if len(open_now) > 6:
            out.append(" - ... a dalších %d" % (len(open_now) - 6))
    else:
        out.append("Otevřené pozice: žádné")
    if new:
        out.append("Za poslední hodinu:")
        for r in new:
            out.append(" - " + trade_text(r))
    else:
        out.append("Za poslední hodinu: žádný obchod")
    out.append("Trh USA: %s | kol za hodinu: %d z %d" % (
        "otevřeno" if us_session_open(t_now) else "zavřeno", count_rows_since(rows, since), int(60 / POLL_MIN)))
    out.extend(data_health_lines(s))
    ready = sum(1 for n in SYMS if len(s["hist"].get(n, [])) > SLOW_N)
    if ready < len(SYMS):
        out.append("Zahřívání: %d z %d nástrojů má dost historie (potřeba %d kol)." % (ready, len(SYMS), SLOW_N + 1))
    return "\n".join(out)


def build_report():
    """Podrobný ranní report."""
    if not os.path.exists(F_STATE):
        return "Zatím nejsou žádná data. Nejdřív musí proběhnout aspoň jedno kolo (--once)."
    s = load_state()
    prices = current_prices(s)
    if not prices:
        return "Zatím se nepodařilo stáhnout žádnou cenu."
    eq, bh, spy = equity(s, prices), buyhold(s, prices), spy_hold(s, prices)
    t_now = now_dt()
    since24 = t_now - timedelta(hours=24)
    rows = eq_rows()
    trades = read_csv(F_TRADES)
    closed = [r for r in trades if r["action"] == "CLOSE"]

    out = []
    out.append("PAPER TRADER (CFD simulace) - podrobný report")
    out.append(local(t_now).strftime("%d.%m.%Y %H:%M") + " (" + REPORT_TZ + ")")

    # ---- účet ----
    out.append("")
    out.append("ÚČET")
    out.append("Hodnota: %s USD (%+.2f %% od startu)" % (money(eq), pct(eq, START_CASH)))
    b24 = row_at_or_before(rows, since24)
    parts = []
    if b24:
        parts.append("24 h: %+.2f %%" % pct(eq, b24[1]))
    if rows and rows[0][0] <= t_now - timedelta(days=7):
        b7 = row_at_or_before(rows, t_now - timedelta(days=7))
        parts.append("7 dní: %+.2f %%" % pct(eq, b7[1]))
    parts.append("od startu: %+.2f %%" % pct(eq, START_CASH))
    out.append(" | ".join(parts))
    out.append("Srovnání od startu: kup-a-drž všech %d nástrojů %+.2f %%, SPY %+.2f %%" % (
        len(SYMS), pct(bh, START_CASH), pct(spy, START_CASH)))
    if rows:
        out.append("Největší propad od startu: %.2f %%" % max_drawdown(rows))
        out.append("Graf 24 h: " + sparkline(spark_series(rows, since24, 24)))
        if rows[0][0] <= t_now - timedelta(days=2):
            out.append("Graf 7 dní: " + sparkline(spark_series(rows, t_now - timedelta(days=7), 28)))

    # ---- obchody ----
    out.append("")
    out.append("OBCHODY")
    pnls = [float(r["pnl_usd"]) for r in closed]
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x <= 0]
    if pnls:
        out.append("Zavřených: %d, ziskových: %d (%.0f %%)" % (len(pnls), len(wins), len(wins) / len(pnls) * 100))
        avg_w = sum(wins) / len(wins) if wins else 0.0
        avg_l = sum(losses) / len(losses) if losses else 0.0
        pf = "%.2f" % (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else "-"
        out.append("Průměrný zisk %+.2f USD, průměrná ztráta %+.2f USD, profit factor %s" % (avg_w, avg_l, pf))
        best = max(closed, key=lambda r: float(r["pnl_usd"]))
        worst = min(closed, key=lambda r: float(r["pnl_usd"]))
        out.append("Nejlepší: %s %+.2f USD, nejhorší: %s %+.2f USD" % (
            best["symbol"], float(best["pnl_usd"]), worst["symbol"], float(worst["pnl_usd"])))
    else:
        out.append("Zatím žádný zavřený obchod.")
    last = [r for r in trades if parse_ts(r["time"]) > since24]
    if last:
        out.append("Za posledních 24 h (%d):" % len(last))
        for r in last[-8:]:
            out.append(" - %s %s" % (local(parse_ts(r["time"])).strftime("%H:%M"), trade_text(r)))
        if len(last) > 8:
            out.append(" - (dalších %d starších obchodů vynecháno)" % (len(last) - 8))
    else:
        out.append("Za posledních 24 h: žádné obchody")

    # ---- po nástrojích ----
    out.append("")
    out.append("VÝSLEDKY PO NÁSTROJÍCH (zavřené + otevřené, od startu)")
    per = {n: [0.0, 0] for n in SYMS}
    for r in closed:
        if r["symbol"] in per:
            per[r["symbol"]][0] += float(r["pnl_usd"])
            per[r["symbol"]][1] += 1
    for n, p in s["pos"].items():
        if n in per and n in prices:
            per[n][0] += pos_pnl(p, prices[n]) - p["fin_paid"]
    for n, (v, c) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        flag = " (otevřeno %s)" % s["pos"][n]["side"] if n in s["pos"] else ""
        out.append("%-5s %+8.2f USD, zavřených %d%s" % (n, v, c, flag))

    # ---- pozice ----
    out.append("")
    out.append("POZICE")
    used = used_margin(s)
    gross = sum(p["units"] * prices.get(n, p["mid_entry"]) for n, p in s["pos"].items())
    out.append("Otevřeno %d z %d, celková hodnota %s USD (%.0f %% účtu)" % (
        len(s["pos"]), len(SYMS), money(gross), gross / eq * 100 if eq > 0 else 0))
    if used > 0:
        out.append("Použitá marže %s USD, úroveň marže %.0f %% (close-out při %.0f %%)" % (
            money(used), eq / used * 100, CLOSEOUT_LEVEL * 100))
    for n in s["pos"]:
        out.append(" - " + pos_line(s, n, prices.get(n, s["pos"][n]["mid_entry"])))

    # ---- náklady a data ----
    out.append("")
    out.append("NÁKLADY A DATA")
    costs = s["spread_paid"] + s["financing_paid"] + s["fx_paid"]
    out.append("Spread %s USD, noční poplatky %s USD, FX poplatek %s USD (součet %s USD, %.2f %% účtu)" % (
        money(s["spread_paid"]), money(s["financing_paid"]), money(s["fx_paid"]),
        money(costs), costs / START_CASH * 100))
    out.append("Kol za posledních 24 h: %d z %d (méně = GitHub některá přeskočil)" % (
        count_rows_since(rows, since24), TICKS_PER_DAY))
    out.extend(data_health_lines(s))
    ready = sum(1 for n in SYMS if len(s["hist"].get(n, [])) > SLOW_N)
    if ready < len(SYMS):
        out.append("Zahřívání: %d z %d nástrojů má dost historie." % (ready, len(SYMS)))
    if len(closed) < 30:
        out.append("Pozor: jen %d zavřených obchodů, to je zatím málo na jakýkoli závěr." % len(closed))
    out.append("Virtuální peníze, žádný skutečný obchod. Spread a noční poplatky podle zadaných údajů Trading 212 (průměry, skutečný spread kolísá).")
    return "\n".join(out)


def send_telegram(text, exit_on_error=True):
    """Pošle zprávu (dlouhé dělí na části). Vrací True/False; při exit_on_error=True chyba ukončí program."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    def fail(msg):
        print("CHYBA: " + msg)
        if exit_on_error:
            sys.exit(1)
        return False

    if not token or not chat:
        return fail("chybí TELEGRAM_TOKEN nebo TELEGRAM_CHAT_ID (Settings > Secrets > Actions).")
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for part in chunks:
        data = urllib.parse.urlencode({"chat_id": chat, "text": part.rstrip("\n")}).encode("utf-8")
        req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=data)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                ok = json.loads(r.read().decode("utf-8")).get("ok")
        except Exception as e:
            return fail("odeslání na Telegram selhalo: %s" % short_err(e))
        if not ok:
            return fail("Telegram odpověděl, že zprávu nepřijal.")
    print("Zpráva odeslána na Telegram (%d část/i)." % len(chunks))
    return True


def maybe_send_hourly(s):
    """Jednou za hodinu (první kolo v nové hodině UTC) pošle krátký přehled."""
    key = now_dt().strftime("%Y-%m-%d %H")
    if s.get("last_report") == key:
        return
    text = build_short_report()
    if DRY:
        print(text)
        return
    if send_telegram(text, exit_on_error=False):
        s["last_report"] = key
        save_state(s)


def check_sources():
    t = now_dt()
    print("Kontrola zdrojů dat. Čas UTC: %s, trh USA: %s" % (
        now(), "otevřeno" if us_session_open(t) else "zavřeno (u akcií bude stáří dat velké)"))
    print("Finnhub klíč: %s" % ("nastaven" if os.environ.get("FINNHUB_API_KEY", "").strip() else "CHYBÍ"))
    bad = 0
    for sym in SYMS:
        try:
            price, age = real_quote(sym)
            print("%-5s %12s USD  stáří dat %s" % (sym, money(price), "%d min" % (age / 60) if age else "živá cena"))
        except Exception as e:
            bad += 1
            print("%-5s CHYBA: %s" % (sym, e))
        if INFO[sym]["src"] == "us":
            time.sleep(0.1)
    print("Hotovo: %d z %d nástrojů v pořádku." % (len(SYMS) - bad, len(SYMS)))
    if bad:
        sys.exit(1)


# ------------------------------ main ------------------------------
def main():
    make_report = build_short_report if "--short" in sys.argv else build_report

    if "--check" in sys.argv:
        check_sources()
        return

    if "--report" in sys.argv:
        print(make_report())
        return

    if "--telegram" in sys.argv:
        text = make_report()
        if DRY:
            print(text)
        else:
            send_telegram(text)
        return

    quote_fn = fake_quote if DEMO else real_quote

    if DEMO and "--once" not in sys.argv:
        for p in (F_STATE, F_TRADES, F_EQUITY):
            if os.path.exists(p):
                os.remove(p)
        print("DEMO: %d kol po %d minutách na vymyšlených cenách (jen test mechaniky)." % (DEMO_TICKS, POLL_MIN))
        t0 = datetime.now(timezone.utc) - timedelta(minutes=POLL_MIN * DEMO_TICKS)
        _clock[0] = t0
        s = load_state()
        for i in range(DEMO_TICKS):
            _clock[0] = t0 + timedelta(minutes=POLL_MIN * i)
            tick(s, quote_fn, show=(i % 600 == 0))
        _clock[0] = None
        save_state(s)
        print("")
        print(build_report())
        return

    # --once: jedno kolo
    s = load_state()
    if not tick(s, quote_fn):
        print("CHYBA: nepodařilo se stáhnout žádnou cenu.")
        sys.exit(1)
    save_state(s)
    if "--notify" in sys.argv:
        maybe_send_hourly(s)


if __name__ == "__main__":
    main()
