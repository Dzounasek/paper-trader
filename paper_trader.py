#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paper_trader.py (serverová verze) - papírový obchodník. ŽÁDNÉ SKUTEČNÉ PENÍZE.

Každé spuštění udělá JEDNO kolo: stáhne cenu, rozhodne podle pravidla
(křížení dvou klouzavých průměrů + stop-loss), obchoduje s VIRTUÁLNÍMI penězi
a uloží stav do složky data/. Plánovač (GitHub Actions) ho pouští každou hodinu.

Režimy:
  python3 paper_trader.py --once       jedno kolo (to pouští server každou hodinu)
  python3 paper_trader.py --telegram   pošle denní report na Telegram
  python3 paper_trader.py --report     vypíše report do okna (nic neposílá)
  python3 paper_trader.py --demo       rychlý test na vymyšlených cenách (data/demo_*)
Přepínače navíc: --dry (u --telegram jen vypíše, neodešle), --once s --demo = 1 kolo naoko.

Používá jen standardní knihovny Pythonu, nic se neinstaluje.
"""

import csv
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

# ========================= NASTAVENÍ =========================
SYMBOLS = ["BTC", "ETH"]     # co sledujeme (kryptoměny, trh jede 24/7)
START_CASH = 10000.0         # virtuální rozpočet v USD
FAST_N = 6                   # krátký klouzavý průměr (počet kol = hodin)
SLOW_N = 24                  # dlouhý klouzavý průměr (počet kol = hodin)
FEE_PCT = 0.001              # poplatek 0,1 % z každého obchodu
SLIPPAGE_PCT = 0.0005        # prokluz 0,05 % v neprospěch (horší cena než viděná)
STOP_LOSS_PCT = 0.05         # prodej, když cena klesne o 5 % pod nákupní
REPORT_TZ = "Europe/Prague"  # časová zóna, ve které se píší časy v reportu
DEMO_TICKS = 1500            # kolik hodinových kol proběhne v režimu --demo
# =============================================================

DEMO = "--demo" in sys.argv
DRY = "--dry" in sys.argv
PREFIX = "demo_" if DEMO else ""
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
F_STATE = os.path.join(DATA_DIR, PREFIX + "state.json")
F_TRADES = os.path.join(DATA_DIR, PREFIX + "trades.csv")
F_EQUITY = os.path.join(DATA_DIR, PREFIX + "equity.csv")

_clock = [None]          # v demu "falešné hodiny", jinak se bere skutečný čas
ERRORS = []              # poslední chyby při stahování cen (pro výpis)


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


# ---------------------------- ceny ----------------------------
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "paper-trader/2.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def get_price(name):
    """Cena z Coinbase, když selže, tak z Binance. Při chybě vrací None."""
    try:
        d = fetch_json("https://api.coinbase.com/v2/prices/%s-USD/spot" % name)
        return float(d["data"]["amount"])
    except Exception as e:
        ERRORS.append("Coinbase %s: %r" % (name, e))
    try:
        d = fetch_json("https://api.binance.com/api/v3/ticker/price?symbol=%sUSDT" % name)
        return float(d["price"])
    except Exception as e:
        ERRORS.append("Binance %s: %r" % (name, e))
    return None


_fake = {}


def fake_price(name):
    """Vymyšlená cena (náhodná procházka) pro režim --demo."""
    start = {"BTC": 30000.0, "ETH": 2000.0}.get(name, 100.0)
    p = _fake.get(name, start) * (1 + random.gauss(0, 0.006))
    _fake[name] = p
    return p


# ---------------------------- stav ----------------------------
def new_state():
    return {"cash": START_CASH, "pos": {}, "start_prices": {}, "hist": {},
            "n_ticks": 0, "started": now()}


def load_state():
    if os.path.exists(F_STATE):
        with open(F_STATE) as f:
            return json.load(f)
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


def equity(s, prices):
    return s["cash"] + sum(p["qty"] * prices[n] for n, p in s["pos"].items() if n in prices)


def buyhold(s, prices):
    per = START_CASH / len(SYMBOLS)
    total = 0.0
    for n in SYMBOLS:
        if n in prices and n in s["start_prices"]:
            total += per * prices[n] / s["start_prices"][n]
        else:
            total += per  # zatím bez ceny = bereme jako beze změny
    return total


# --------------------------- obchodování ---------------------------
TRADE_HEADER = ["time", "symbol", "side", "price", "qty", "fee_usd", "pnl_usd", "reason"]


def buy(s, name, price, reason):
    stake = min(s["cash"], START_CASH / len(SYMBOLS))
    if stake < 10:
        return
    fill = price * (1 + SLIPPAGE_PCT)
    fee = stake * FEE_PCT
    qty = (stake - fee) / fill
    s["cash"] -= stake
    s["pos"][name] = {"qty": qty, "entry": fill, "cost": stake}
    append_csv(F_TRADES, TRADE_HEADER,
               [now(), name, "BUY", round(fill, 4), round(qty, 8), round(fee, 4), "", reason])


def sell(s, name, price, reason):
    p = s["pos"].pop(name)
    fill = price * (1 - SLIPPAGE_PCT)
    gross = p["qty"] * fill
    fee = gross * FEE_PCT
    s["cash"] += gross - fee
    pnl = gross - fee - p["cost"]
    append_csv(F_TRADES, TRADE_HEADER,
               [now(), name, "SELL", round(fill, 4), round(p["qty"], 8),
                round(fee, 4), round(pnl, 4), reason])


def sma(xs, n):
    return sum(xs[-n:]) / n


def trade_logic(s, name, h):
    """TADY JE STRATEGIE. Později můžeš nahradit jiným pravidlem."""
    price = h[-1]
    pos = s["pos"].get(name)
    if pos and price <= pos["entry"] * (1 - STOP_LOSS_PCT):
        sell(s, name, price, "stop-loss")
        return
    if len(h) < SLOW_N + 1:
        return  # ještě nemáme dost historie
    fast, slow = sma(h, FAST_N), sma(h, SLOW_N)
    fast0, slow0 = sma(h[:-1], FAST_N), sma(h[:-1], SLOW_N)
    if pos and fast0 >= slow0 and fast < slow:
        sell(s, name, price, "krizeni dolu")
    elif (not pos) and fast0 <= slow0 and fast > slow:
        buy(s, name, price, "krizeni nahoru")


def tick(s, price_fn, show=True):
    """Jedno kolo. Vrací True, když se podařilo stáhnout aspoň jednu cenu."""
    s["n_ticks"] += 1
    got = 0
    for name in SYMBOLS:
        price = price_fn(name)
        if price is None:
            continue
        got += 1
        h = s["hist"].setdefault(name, [])
        h.append(price)
        del h[:-(SLOW_N + 10)]          # držíme jen posledních pár cen
        s["start_prices"].setdefault(name, price)
        trade_logic(s, name, h)
    if got == 0:
        s["n_ticks"] -= 1
        return False
    prices = {n: h[-1] for n, h in s["hist"].items() if h}
    eq, bh = equity(s, prices), buyhold(s, prices)
    append_csv(F_EQUITY, ["time", "equity_usd", "buyhold_usd"], [now(), round(eq, 2), round(bh, 2)])
    if show:
        warm = min(len(s["hist"].get(n, [])) for n in SYMBOLS)
        cenyt = "  ".join("%s %.2f" % (n, prices[n]) for n in SYMBOLS if n in prices)
        pozice = ",".join(s["pos"].keys()) or "-"
        zahrivani = "" if warm > SLOW_N else " | zahřívání %d/%d" % (warm, SLOW_N + 1)
        print("%s UTC | %s | portfolio %.2f (%+.2f %%) | kup-a-drž %+.2f %% | pozice: %s%s" % (
            now(), cenyt, eq, (eq / START_CASH - 1) * 100,
            (bh / START_CASH - 1) * 100, pozice, zahrivani))
    return True


# ----------------------------- report -----------------------------
def money(x):
    return "{:,.2f}".format(x).replace(",", " ")


def build_report():
    if not os.path.exists(F_STATE):
        return "Zatím nejsou žádná data. Nejdřív musí proběhnout aspoň jedno kolo (--once)."
    s = load_state()
    prices = {n: h[-1] for n, h in s["hist"].items() if h}
    if not prices:
        return "Zatím se nepodařilo stáhnout žádnou cenu."
    eq, bh = equity(s, prices), buyhold(s, prices)
    t_now = now_dt()
    since = t_now - timedelta(hours=24)

    # --- změna za posledních 24 h (z equity.csv) ---
    rows = read_csv(F_EQUITY)
    base = None
    for r in rows:
        if parse_ts(r["time"]) <= since:
            base = r
        else:
            break
    if base is None and rows:
        base = rows[0]
    day_line = ""
    if base:
        e0, b0 = float(base["equity_usd"]), float(base["buyhold_usd"])
        day_line = "Posledních 24 h: strategie %+.2f %%, kup a drž %+.2f %%" % (
            (eq / e0 - 1) * 100, (bh / b0 - 1) * 100)
    ticks24 = sum(1 for r in rows if parse_ts(r["time"]) > since)

    # --- pozice ---
    pos_txt = []
    for n in SYMBOLS:
        p = s["pos"].get(n)
        if p and n in prices:
            pos_txt.append("%s %+.1f %% (nákup %s)" % (n, (prices[n] / p["entry"] - 1) * 100, money(p["entry"])))
        else:
            pos_txt.append("%s bez pozice" % n)

    # --- obchody ---
    trades = read_csv(F_TRADES)
    last = [r for r in trades if parse_ts(r["time"]) > since]
    sells = [r for r in trades if r["side"] == "SELL"]
    wins = sum(1 for r in sells if float(r["pnl_usd"]) > 0)
    fees = sum(float(r["fee_usd"]) for r in trades)

    out = []
    out.append("PAPER TRADER - denní report")
    out.append(local(t_now).strftime("%d.%m.%Y %H:%M") + " (" + REPORT_TZ + ")")
    out.append("")
    out.append("Portfolio: %s USD (%+.2f %% od startu)" % (money(eq), (eq / START_CASH - 1) * 100))
    out.append("Kup a drž: %s USD (%+.2f %% od startu)" % (money(bh), (bh / START_CASH - 1) * 100))
    if day_line:
        out.append(day_line)
    out.append("")
    out.append("Pozice: " + "; ".join(pos_txt))
    if last:
        out.append("Obchody za 24 h:")
        for r in last:
            extra = ""
            if r["side"] == "SELL":
                extra = " (výsledek %+.2f USD, %s)" % (float(r["pnl_usd"]), r["reason"])
            out.append(" - %s %s %s @ %s%s" % (
                local(parse_ts(r["time"])).strftime("%H:%M"), r["side"], r["symbol"],
                money(float(r["price"])), extra))
    else:
        out.append("Obchody za 24 h: žádné")
    out.append("")
    out.append("Celkem uzavřených obchodů: %d, ziskových: %d, poplatky celkem: %s USD" % (
        len(sells), wins, money(fees)))
    out.append("Kol za posledních 24 h: %d z 24 (méně = GitHub některá přeskočil)" % ticks24)
    warm = min(len(s["hist"].get(n, [])) for n in SYMBOLS)
    if warm <= SLOW_N:
        out.append("Zahřívání: zatím %d/%d cen, obchodovat začne až potom." % (warm, SLOW_N + 1))
    if len(sells) < 30:
        out.append("Pozor: jen %d uzavřených obchodů, to je zatím málo na jakýkoli závěr." % len(sells))
    out.append("Virtuální peníze, žádný skutečný obchod.")
    return "\n".join(out)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("CHYBA: chybí TELEGRAM_TOKEN nebo TELEGRAM_CHAT_ID (Settings > Secrets > Actions).")
        print(text)
        sys.exit(1)
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = json.loads(r.read().decode("utf-8")).get("ok")
    except Exception as e:
        print("CHYBA: odeslání na Telegram selhalo: %s" % type(e).__name__)
        sys.exit(1)
    if not ok:
        print("CHYBA: Telegram odpověděl, že zprávu nepřijal.")
        sys.exit(1)
    print("Report odeslán na Telegram.")


# ------------------------------ main ------------------------------
def main():
    if "--report" in sys.argv:
        print(build_report())
        return

    if "--telegram" in sys.argv:
        text = build_report()
        if DRY:
            print(text)
        else:
            send_telegram(text)
        return

    price_fn = fake_price if DEMO else get_price

    if DEMO and "--once" not in sys.argv:
        for p in (F_STATE, F_TRADES, F_EQUITY):
            if os.path.exists(p):
                os.remove(p)
        print("DEMO: %d hodinových kol na vymyšlených cenách (jen test mechaniky)." % DEMO_TICKS)
        t0 = datetime.now(timezone.utc) - timedelta(hours=DEMO_TICKS)
        _clock[0] = t0
        s = load_state()
        for i in range(DEMO_TICKS):
            _clock[0] = t0 + timedelta(hours=i)
            tick(s, price_fn, show=(i % 300 == 0))
        _clock[0] = None
        save_state(s)
        print("")
        print(build_report())
        return

    # --once: jedno kolo
    s = load_state()
    if not tick(s, price_fn):
        print("CHYBA: nepodařilo se stáhnout žádnou cenu. Důvody:")
        for e in ERRORS[-4:]:
            print("  " + e)
        sys.exit(1)
    save_state(s)


if __name__ == "__main__":
    main()
