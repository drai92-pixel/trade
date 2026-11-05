#!/usr/bin/env python3
"""Adjusted cost base (ACB) and capital gains for trader-tax, Canadian rules.
Standard library only; the only network call is `fx`, to the Bank of Canada.

Not tax advice: it does arithmetic on the purchases you give it. Your
accountant decides what applies to you.

    python3 lots.py report                          # ACB, value and unrealized gain per holding (CAD)
    python3 lots.py report --lots                   # plus every purchase
    python3 lots.py sell KO 2 --price 88.50         # what selling 2 KO would realize, in CAD
    python3 lots.py sell KO 2 --price 88.50 --fx 1.39 --fee 1 --record   # it filled: update ACB + log
    python3 lots.py harvest --min-loss 100          # tax-loss selling candidates (CAD)
    python3 lots.py harvest --min-loss 100 --write  # write them to trader/orders.json as proposals
    python3 lots.py wash KO                         # would buying KO today create a superficial loss?
    python3 lots.py fx --date 2026-10-13 --write    # Bank of Canada USD/CAD rate into trader/prices.json

Holdings live in trader/portfolio.json:

    {"cash": 500.0, "positions": [
      {"ticker": "KO", "account": "personal", "currency": "USD", "shares": 7.8571,
       "lots": [{"id": "KO-1", "date": "2026-10-13", "shares": 7.8571, "cost": 708.0,
                 "fx": 1.3812, "fee": 1.0}]}]}

  cost      total paid for the lot, in the stock's own currency
  fx        CAD per 1 unit of that currency on the trade date (the Bank of
            Canada daily rate is what CRA accepts); not needed for CAD stocks
  fee       commission, same currency; it is added to the ACB
  account   personal (default, non-registered), corp (your professional
            corporation's non-registered account), tfsa, rrsp, rrif, fhsa,
            resp, spouse
  currency  default CAD for .TO .V .NE .CN tickers, otherwise USD

The rules it applies (federal Income Tax Act, the arithmetic only):
  ACB          average cost. Every purchase of identical shares owned by the
               same taxpayer goes into one pool, across all their
               non-registered accounts and both listings of an interlisted
               stock (RY and RY.TO are the same shares). Lot dates do not
               change the gain; there is no FIFO and no short/long term.
  CAD          cost and proceeds are each converted to CAD at the rate on
               their own date, so currency moves are part of the gain.
  inclusion    half of a net capital gain is taxable (the taxable capital
               gain). Net capital losses carry back 3 years or forward
               indefinitely, against capital gains only.
  superficial  a loss is denied if you, your spouse, or a corporation you
  loss         control buy identical shares (in ANY account, TFSA and RRSP
               included) in the 30 days before or after the sale and still
               hold them 30 days after it. Denied part = loss x min(shares
               sold, shares bought in the window, shares held at the end) /
               shares sold. It is added to the ACB of the shares bought back,
               except in a registered account, where it is lost for good.
  registered   TFSA, RRSP, RRIF, FHSA, RESP: no capital gains or losses at
               all. They count only for the superficial loss check.
  corp         a separate taxpayer: its own ACB pool, but affiliated with you
               for superficial losses.
"""

import argparse
import datetime
import json
import os
import sys
import urllib.request

REGISTERED = {"tfsa", "rrsp", "rrif", "fhsa", "resp", "lira", "rdsp"}
TAXABLE = {"personal": "you", "corp": "your corporation"}
ACCOUNTS = REGISTERED | set(TAXABLE) | {"spouse"}
CAD_SUFFIXES = (".TO", ".V", ".NE", ".CN")
# Identical property: share classes treated as one, and interlisted stocks
# whose US ticker differs from the TSX one. Same-ticker interlistings (RY and
# RY.TO) come from INTERLISTED.
FAMILIES = [{"GOOGL", "GOOG"}, {"BRK.A", "BRK.B", "BRK-A", "BRK-B"}, {"CNI", "CNR.TO"},
            {"TU", "T.TO"}]
INTERLISTED = {"RY", "TD", "BMO", "BNS", "CM", "ENB", "CNQ", "SU", "CP", "SHOP", "BCE", "TRP",
               "MFC", "SLF", "NTR", "BN", "BAM", "TRI", "CVE", "IMO", "WCN", "GIB", "OTEX",
               "CCO", "ABX", "AEM", "WPM", "FNV", "TECK", "MG", "CSU", "BIP", "BEP", "QSR"}
EPS = 1e-9
BOC_URL = "https://www.bankofcanada.ca/valet/observations/FX%sCAD/json?start_date=%s&end_date=%s"


class TaxError(Exception):
    pass


def read_json(path, what, required=True):
    if not path or not os.path.exists(path):
        if required:
            raise TaxError("no %s at %s" % (what, path))
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise TaxError("%s is not valid JSON: %s" % (path, e))


def write_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def day(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        raise TaxError("not a date (YYYY-MM-DD): %r" % s)


def money(x):
    x = float(x)
    sign = "-" if x < -0.004 else ""
    return "%s$%s" % (sign, "{:,.2f}".format(abs(x)))


def fmt_qty(x):
    s = ("%.6f" % float(x)).rstrip("0").rstrip(".")
    return s or "0"


def base(ticker):
    t = ticker.upper()
    for suf in CAD_SUFFIXES:
        if t.endswith(suf):
            return t[:-len(suf)]
    return t


def family(ticker):
    """Every ticker that is the same (identical) shares as this one."""
    t = ticker.upper()
    for f in FAMILIES:
        if t in f:
            return set(f)
    b = base(t)
    if b in INTERLISTED:
        return {b, b + ".TO"}
    return {t}


def family_key(ticker):
    return min(family(ticker))


def currency_of(pos):
    c = pos.get("currency")
    if c:
        return str(c).upper()
    t = str(pos.get("ticker") or pos.get("symbol") or "").upper()
    return "CAD" if any(t.endswith(s) for s in CAD_SUFFIXES) else "USD"


def account_of(pos):
    a = str(pos.get("account") or "personal").lower()
    if a not in ACCOUNTS:
        raise TaxError("%s: unknown account %r (use one of: %s)"
                       % (pos.get("ticker"), a, ", ".join(sorted(ACCOUNTS))))
    return a


def get_fx(prices_doc, currency, given=None):
    """CAD per unit of currency, for valuing or selling today."""
    if currency == "CAD":
        return 1.0
    if given:
        return float(given)
    fx = (prices_doc or {}).get("fx") or {}
    for k in (currency, currency + "CAD"):
        if k in fx:
            return float(fx[k])
    raise TaxError("no %s->CAD rate: pass --fx, or run `lots.py fx --write` (Bank of Canada rate)" % currency)


# ---------------------------------------------------------------- holdings

def holdings(portfolio):
    """[holding] with parsed lots, ACB in CAD, account and pool key."""
    out = []
    for p in (portfolio or {}).get("positions", []):
        t = str(p.get("ticker") or p.get("symbol") or "").upper()
        if not t:
            continue
        acct, cur = account_of(p), currency_of(p)
        lots, acb = [], 0.0
        for i, l in enumerate(p.get("lots", []), 1):
            sh = float(l.get("shares", 0))
            if sh <= EPS:
                continue
            lid = l.get("id") or "%s-%d" % (t, i)
            if cur == "CAD":
                fx = 1.0
            elif l.get("fx") is None:
                if acct in TAXABLE:
                    raise TaxError("purchase %s (%s) has no fx rate: add \"fx\" (CAD per %s on %s; "
                                   "`lots.py fx --date %s`)" % (lid, t, cur, l.get("date"), l.get("date")))
                fx = None
            else:
                fx = float(l["fx"])
            cost, fee = float(l.get("cost", 0)), float(l.get("fee", 0) or 0)
            lots.append({"id": lid, "date": day(l.get("date")), "shares": sh, "cost": cost, "fee": fee, "fx": fx})
            if fx is not None:
                acb += (cost + fee) * fx
        if not lots and float(p.get("shares", 0) or 0) > EPS:
            raise TaxError("%s has shares but no purchases; add its lots (date, shares, cost, fx) to "
                           "trader/portfolio.json" % t)
        acb += float(p.get("acb_adj_cad", 0) or 0)
        lots.sort(key=lambda l: (l["date"], l["id"]))
        out.append({"ticker": t, "account": acct, "currency": cur, "lots": lots,
                    "shares": sum(l["shares"] for l in lots), "acb": acb, "raw": p,
                    "pool": (acct, family_key(t)) if acct in TAXABLE else None})
    return out


def pool_of(hs, owner, ticker):
    """Shares and ACB (CAD) of one taxpayer's pool of identical shares."""
    key = (owner, family_key(ticker))
    members = [h for h in hs if h["pool"] == key]
    sh = sum(h["shares"] for h in members)
    acb = sum(h["acb"] for h in members)
    return {"shares": sh, "acb": acb, "per_share": acb / sh if sh > EPS else 0.0}


def find(hs, ticker, account):
    t = ticker.upper()
    m = [h for h in hs if h["ticker"] == t and (account is None or h["account"] == account)]
    if not m:
        raise TaxError("no %s holding%s in the portfolio" % (t, "" if account is None else " in " + account))
    if len(m) > 1:
        raise TaxError("%s is held in several accounts (%s): pass --account"
                       % (t, ", ".join(h["account"] for h in m)))
    return m[0]


def price_of(prices, ticker):
    return prices.get(ticker) or prices.get(base(ticker))


def unrealized(hs, prices, prices_doc):
    rows, missing = [], []
    for h in hs:
        p = price_of(prices, h["ticker"])
        if not p:
            missing.append(h["ticker"])
            continue
        fx = get_fx(prices_doc, h["currency"])
        value = h["shares"] * p * fx
        row = {"ticker": h["ticker"], "account": h["account"], "currency": h["currency"],
               "shares": round(h["shares"], 6), "price": p, "fx": fx, "value_cad": round(value, 2)}
        if h["pool"]:
            pool = pool_of(hs, h["account"], h["ticker"])
            acb = pool["per_share"] * h["shares"]
            row.update({"acb_cad": round(acb, 2), "acb_per_share_cad": round(pool["per_share"], 4),
                        "gain_cad": round(value - acb, 2),
                        "pct": round(100.0 * (value - acb) / acb, 2) if acb else 0.0})
        else:
            row.update({"acb_cad": None, "gain_cad": None, "pct": None,
                        "note": "registered: no capital gain or loss" if h["account"] in REGISTERED
                        else "spouse's holding: not your gain"})
        rows.append(row)
    return rows, missing


# ---------------------------------------------------------------- superficial loss

def planned_buys(orders, prices):
    out = []
    for o in orders or []:
        if str(o.get("side", "")).lower() != "buy":
            continue
        t = str(o.get("symbol", "")).upper()
        sh = None
        if o.get("qty") is not None:
            sh = float(o["qty"])
        elif o.get("notional") is not None and price_of(prices, t):
            sh = float(o["notional"]) / price_of(prices, t)
        out.append({"ticker": t, "shares": sh})
    return out


def superficial(ticker, sold_on, shares_sold, hs, orders, prices):
    """Shares sold that have a substituted property (CRA's three-way minimum).

    Looks across every account listed (yours, spouse, corp, TFSA, RRSP...).
    Returns (substitute shares, or None when a planned buy has no share count; evidence)."""
    fam = family(ticker)
    lo, hi = sold_on - datetime.timedelta(days=30), sold_on + datetime.timedelta(days=30)
    bought, held, ev, unknown = 0.0, 0.0, [], False
    for h in hs:
        if h["ticker"] not in fam:
            continue
        held += h["shares"]
        for l in h["lots"]:
            if lo <= l["date"] <= hi:
                bought += l["shares"]
                ev.append({"ticker": h["ticker"], "account": h["account"], "date": l["date"].isoformat(),
                           "shares": round(l["shares"], 6), "source": "purchase %s" % l["id"]})
    held -= shares_sold
    for b in planned_buys(orders, prices):
        if b["ticker"] in fam:
            ev.append({"ticker": b["ticker"], "account": "?", "date": "planned", "shares": b["shares"],
                       "source": "a planned buy in trader/orders.json"})
            if b["shares"] is None:
                unknown = True
            else:
                bought += b["shares"]
                held += b["shares"]
    if unknown:
        return None, ev
    sub = max(0.0, min(shares_sold, bought, held))
    return sub, (ev if sub > EPS else [])


def denied_loss(loss, shares_sold, sub):
    if loss >= 0 or shares_sold <= EPS:
        return 0.0
    if sub is None:
        return round(-loss, 2)
    return round(-loss * min(1.0, sub / shares_sold), 2)


def where_denied_goes(ticker, account, ev):
    """The denied loss is added to the ACB of the substitute shares."""
    accts = {e["account"] for e in ev}
    if accts & REGISTERED:
        return "lost for good: the shares bought back are in a %s" % "/".join(sorted(accts & REGISTERED)).upper()
    if "spouse" in accts:
        return "added to the ACB of your spouse's shares"
    if "corp" in accts and account != "corp":
        return "added to the ACB of your corporation's shares"
    return "added to the ACB of the %s shares you keep (a deferral, not a loss)" % ticker


def rebuy_date(sold):
    return (sold + datetime.timedelta(days=31)).isoformat()


# ---------------------------------------------------------------- sales

def simulate_sale(hs, h, shares, price, fx, fee, sold_on, orders=None, prices=None):
    if not h["pool"]:
        raise TaxError("%s is in a %s account: selling it has no capital gain or loss to work out"
                       % (h["ticker"], h["account"].upper()))
    if shares > h["shares"] + EPS:
        raise TaxError("selling %s shares but only %s are held in %s"
                       % (fmt_qty(shares), fmt_qty(h["shares"]), h["account"]))
    pool = pool_of(hs, h["account"], h["ticker"])
    acb_used = pool["per_share"] * shares
    proceeds = price * shares * fx
    outlays = (fee or 0.0) * fx
    gain = proceeds - outlays - acb_used
    res = {"ticker": h["ticker"], "account": h["account"], "shares": round(shares, 6), "price": price,
           "fx": fx, "proceeds_cad": round(proceeds, 2), "outlays_cad": round(outlays, 2),
           "acb_cad": round(acb_used, 2), "acb_per_share_cad": round(pool["per_share"], 4),
           "gain_cad": round(gain, 2), "superficial": [], "denied_cad": 0.0}
    if gain < 0:
        sub, ev = superficial(h["ticker"], sold_on, shares, hs, orders, prices or {})
        res["superficial"] = ev
        res["denied_cad"] = denied_loss(gain, shares, sub)
        if res["denied_cad"]:
            res["denied_goes"] = where_denied_goes(h["ticker"], h["account"], ev)
    res["allowed_cad"] = round(res["gain_cad"] + res["denied_cad"], 2)
    res["taxable_cad"] = round(res["allowed_cad"] / 2.0, 2)
    return res


def _lots_acb(p, currency):
    return sum((float(l["cost"]) + float(l.get("fee", 0) or 0)) *
               (1.0 if currency == "CAD" else float(l.get("fx") or 0)) for l in p.get("lots", []))


def record_sale(portfolio, h, sale, sold_on, state):
    """Remove the shares at average cost: every purchase of this holding shrinks
    pro rata, and acb_adj_cad makes the ACB left behind exact."""
    p = h["raw"]
    f = sale["shares"] / h["shares"] if h["shares"] > EPS else 1.0
    for l in p.get("lots", []):
        l["shares"] = round(float(l.get("shares", 0)) * (1 - f), 6)
        l["cost"] = round(float(l.get("cost", 0)) * (1 - f), 2)
        if l.get("fee"):
            l["fee"] = round(float(l["fee"]) * (1 - f), 4)
    p["lots"] = [l for l in p.get("lots", []) if float(l["shares"]) > EPS]
    p["shares"] = round(h["shares"] - sale["shares"], 6)
    target = h["acb"] - sale["acb_cad"]
    if sale["denied_cad"] and sale.get("denied_goes", "").startswith("added to the ACB of the"):
        target += sale["denied_cad"]
    if p["shares"] > EPS:
        p["acb_adj_cad"] = round(target - _lots_acb(p, h["currency"]), 2)
    else:
        p.pop("acb_adj_cad", None)
    portfolio["positions"] = [q for q in portfolio.get("positions", []) if float(q.get("shares", 0) or 0) > EPS]
    if h["account"] == "personal":
        portfolio["cash"] = round(float(portfolio.get("cash", 0)) +
                                  sale["shares"] * sale["price"] - sale["outlays_cad"] / sale["fx"], 2)
    rec = dict(sale)
    rec["date"] = sold_on.isoformat()
    rec["symbol"] = sale["ticker"]
    state.setdefault("realized", []).append(rec)
    return portfolio, state


def realized_year(state, year):
    out = {}
    for r in (state or {}).get("realized", []):
        if not str(r.get("date", "")).startswith(str(year)):
            continue
        o = out.setdefault(r.get("account", "personal"), {"gains": 0.0, "losses": 0.0, "denied": 0.0, "count": 0})
        g = float(r.get("allowed_cad", r.get("gain_cad", 0)))
        o["gains" if g >= 0 else "losses"] += g
        o["denied"] += float(r.get("denied_cad", 0))
        o["count"] += 1
    for o in out.values():
        o["net"] = o["gains"] + o["losses"]
        o["taxable"] = o["net"] / 2.0
        for k in ("gains", "losses", "denied", "net", "taxable"):
            o[k] = round(o[k], 2)
    return out


def loss_sales_to_watch(state, today):
    """Loss sales whose 30-day window is still open."""
    out = []
    for r in (state or {}).get("realized", []):
        if float(r.get("gain_cad", 0)) >= 0:
            continue
        d = day(r.get("date"))
        if d <= today <= d + datetime.timedelta(days=30):
            out.append(r)
    return out


def late_buys(state, hs):
    """Recorded loss sales that a later purchase (any account) has made superficial."""
    out = []
    for r in (state or {}).get("realized", []):
        g = float(r.get("gain_cad", 0))
        if g >= 0 or float(r.get("denied_cad", 0)) >= -g - 0.01:
            continue
        d = day(r["date"])
        fam = family(r["symbol"])
        later = [(h, l) for h in hs if h["ticker"] in fam for l in h["lots"]
                 if d < l["date"] <= d + datetime.timedelta(days=30)]
        if later:
            out.append({"sale": r, "buys": [{"ticker": h["ticker"], "account": h["account"],
                                             "date": l["date"].isoformat(), "shares": l["shares"]}
                                            for h, l in later]})
    return out


# ---------------------------------------------------------------- harvest

def harvest(hs, prices, prices_doc, today, min_loss=100.0, min_pct=0.0, orders=None):
    cands = []
    for h in hs:
        if not h["pool"] or h["shares"] <= EPS:
            continue
        p = price_of(prices, h["ticker"])
        if not p:
            continue
        fx = get_fx(prices_doc, h["currency"])
        sale = simulate_sale(hs, h, h["shares"], p, fx, 0.0, today, orders, prices)
        loss = sale["gain_cad"]
        pct = 100.0 * loss / sale["acb_cad"] if sale["acb_cad"] else 0.0
        if loss >= 0 or -loss < min_loss or -pct < min_pct:
            continue
        pool = pool_of(hs, h["account"], h["ticker"])
        cands.append({"ticker": h["ticker"], "account": h["account"], "price": p, "fx": fx,
                      "shares": round(h["shares"], 6), "loss_cad": loss, "pct": round(pct, 2),
                      "denied_cad": sale["denied_cad"], "superficial": sale["superficial"],
                      "denied_goes": sale.get("denied_goes"), "taxable_cad": sale["taxable_cad"],
                      "same_shares_elsewhere": pool["shares"] - h["shares"] > EPS,
                      "rebuy_on_or_after": rebuy_date(today)})
    cands.sort(key=lambda c: c["loss_cad"])
    return cands


def harvest_orders(cands, include_superficial=False):
    out = []
    for c in cands:
        if c["denied_cad"] and not include_superficial:
            continue
        why = ("tax-loss selling proposal (%s account): sell %s %s for an estimated %s capital loss "
               "(%s against taxable gains). Nobody buys %s again in ANY account (TFSA, RRSP, spouse, "
               "corporation) before %s, or the loss is denied as a superficial loss. Not tax advice."
               % (c["account"], fmt_qty(c["shares"]), c["ticker"], money(c["loss_cad"]),
                  money(c["taxable_cad"]), "/".join(sorted(family(c["ticker"]))), c["rebuy_on_or_after"]))
        out.append({"symbol": c["ticker"], "side": "sell", "qty": fmt_qty(c["shares"]),
                    "type": "market", "time_in_force": "day", "account": c["account"], "why": why})
    return out


# ---------------------------------------------------------------- Bank of Canada

def boc_rate(date, currency="USD", opener=urllib.request.urlopen):
    """Bank of Canada daily rate (CAD per unit), or the last business day before."""
    d = day(date)
    start = d - datetime.timedelta(days=7)
    url = BOC_URL % (currency.upper(), start.isoformat(), d.isoformat())
    try:
        with opener(url, timeout=20) as r:
            doc = json.load(r)
    except Exception as e:
        raise TaxError("could not reach the Bank of Canada (%s); look up the rate at "
                       "bankofcanada.ca/rates/exchange/daily-exchange-rates and pass it in" % e)
    key = "FX%sCAD" % currency.upper()
    obs = [o for o in doc.get("observations", []) if o.get(key)]
    if not obs:
        raise TaxError("the Bank of Canada has no %s rate on or before %s" % (currency, d))
    last = sorted(obs, key=lambda o: o["d"])[-1]
    return last["d"], float(last[key]["v"])


# ---------------------------------------------------------------- CLI

NOT_ADVICE = ("Not tax advice: arithmetic on the purchases you listed. Superficial losses also count "
              "purchases by your spouse, your corporation, your TFSA/RRSP and other brokers; it only "
              "sees what is in trader/portfolio.json. Check with your accountant before you act.")


def _inputs(a):
    portfolio = read_json(a.portfolio, "portfolio (purchases)")
    pdoc = read_json(a.prices, "prices", required=False) or {}
    prices = {str(k).upper(): float(v) for k, v in (pdoc.get("prices", pdoc) or {}).items()
              if isinstance(v, (int, float))}
    state = read_json(a.state, "tax state", required=False) or {"realized": []}
    odoc = read_json(a.orders, "orders", required=False) or {}
    orders = odoc.get("orders", []) if isinstance(odoc, dict) else odoc
    today = day(a.today) if a.today else datetime.date.today()
    return portfolio, pdoc, prices, state, orders, today


def cmd_report(a):
    portfolio, pdoc, prices, state, orders, today = _inputs(a)
    hs = holdings(portfolio)
    rows, missing = unrealized(hs, prices, pdoc)
    ytd = realized_year(state, today.year)
    late = late_buys(state, hs)
    if a.json:
        print(json.dumps({"positions": rows, "missing_prices": missing, "realized": ytd,
                          "superficial_after_the_fact": late}, indent=2, default=str))
        return 0
    print("All amounts in CAD.")
    print("%-8s %-9s %10s %12s %12s %12s %8s" % ("ticker", "account", "shares", "ACB", "value", "gain", "gain %"))
    for r in rows:
        if r["acb_cad"] is None:
            print("%-8s %-9s %10s %12s %12s   %s" % (r["ticker"], r["account"], fmt_qty(r["shares"]), "-",
                                                    money(r["value_cad"]), r["note"]))
        else:
            print("%-8s %-9s %10s %12s %12s %12s %7.2f%%" % (r["ticker"], r["account"], fmt_qty(r["shares"]),
                  money(r["acb_cad"]), money(r["value_cad"]), money(r["gain_cad"]), r["pct"]))
    if a.lots:
        print("")
        print("%-10s %-9s %-11s %10s %12s %8s %s" % ("purchase", "account", "date", "shares", "cost+fee", "fx", "cur"))
        for h in hs:
            for l in h["lots"]:
                print("%-10s %-9s %-11s %10s %12s %8s %s" % (l["id"], h["account"], l["date"], fmt_qty(l["shares"]),
                      "{:,.2f}".format(l["cost"] + l["fee"]), "-" if l["fx"] is None else "%.4f" % l["fx"],
                      h["currency"]))
    for t in missing:
        print("No price for %s: add it to trader/prices.json (trader-alerts: alerts.py quote %s --write)" % (t, t))
    print("")
    if not ytd:
        print("No sales recorded in %d." % today.year)
    for acct, o in ytd.items():
        print("Realized in %d, %s: net %s (gains %s, losses %s); taxable half %s; %s denied as superficial."
              % (today.year, TAXABLE.get(acct, acct), money(o["net"]), money(o["gains"]),
                 money(o["losses"]), money(o["taxable"]), money(o["denied"])))
    for x in late:
        s = x["sale"]
        print("WARNING: the %s loss sale on %s now looks superficial: bought back %s. Re-check that loss "
              "with your accountant." % (s["symbol"], s["date"], ", ".join(
                  "%s %s in %s on %s" % (fmt_qty(b["shares"]), b["ticker"], b["account"], b["date"])
                  for b in x["buys"])))
    print(NOT_ADVICE)
    return 0


def cmd_sell(a):
    portfolio, pdoc, prices, state, orders, today = _inputs(a)
    hs = holdings(portfolio)
    h = find(hs, a.ticker, a.account)
    price = a.price or price_of(prices, h["ticker"])
    if not price:
        raise TaxError("no price for %s: pass --price" % h["ticker"])
    fx = get_fx(pdoc, h["currency"], a.fx)
    sale = simulate_sale(hs, h, float(a.shares), float(price), fx, float(a.fee or 0), today, orders, prices)
    if a.record:
        portfolio, state = record_sale(portfolio, h, sale, today, state)
        write_json(a.portfolio, portfolio)
        write_json(a.state, state)
    if a.json:
        print(json.dumps(sale, indent=2))
        return 0
    print("Selling %s %s (%s account) at %s %s, CAD rate %.4f:" % (fmt_qty(sale["shares"]), h["ticker"],
          h["account"], "{:,.2f}".format(float(price)), h["currency"], fx))
    print("  proceeds %s - outlays %s - ACB %s (%s/share) = %s %s"
          % (money(sale["proceeds_cad"]), money(sale["outlays_cad"]), money(sale["acb_cad"]),
             money(sale["acb_per_share_cad"]), "gain" if sale["gain_cad"] >= 0 else "loss",
             money(sale["gain_cad"])))
    if sale["denied_cad"]:
        print("  SUPERFICIAL LOSS: %s of it is denied (%s)." % (money(sale["denied_cad"]), sale["denied_goes"]))
        for e in sale["superficial"]:
            print("    because of %s %s in %s, %s (%s)" % (fmt_qty(e["shares"]) if e["shares"] else "?",
                  e["ticker"], e["account"], e["date"], e["source"]))
    print("  capital %s counted: %s; taxable half: %s" % ("gain" if sale["allowed_cad"] >= 0 else "loss",
          money(sale["allowed_cad"]), money(sale["taxable_cad"])))
    if sale["gain_cad"] < 0:
        print("  To keep the loss, nobody (you, spouse, corporation, TFSA, RRSP) buys %s before %s."
              % ("/".join(sorted(family(h["ticker"]))), rebuy_date(today)))
    if a.record:
        print("Recorded: ACB updated in %s, sale logged in %s." % (a.portfolio, a.state))
    print(NOT_ADVICE)
    return 0


def cmd_harvest(a):
    portfolio, pdoc, prices, state, orders, today = _inputs(a)
    hs = holdings(portfolio)
    cands = harvest(hs, prices, pdoc, today, a.min_loss, a.min_pct, orders)
    if a.json:
        print(json.dumps(cands, indent=2))
    else:
        if not cands:
            print("No holding in a taxable account is down more than %s (CAD)." % money(a.min_loss))
        for c in cands:
            print("%s (%s): sell %s for about %s (%.1f%%); taxable half %s; rebuy on or after %s"
                  % (c["ticker"], c["account"], fmt_qty(c["shares"]), money(c["loss_cad"]), c["pct"],
                     money(c["taxable_cad"]), c["rebuy_on_or_after"]))
            if c["denied_cad"]:
                print("   SUPERFICIAL: %s would be denied (%s). Not proposed." % (money(c["denied_cad"]),
                                                                             c["denied_goes"]))
            if c["same_shares_elsewhere"]:
                print("   The same shares are also held under another listing in this account; the ACB is pooled.")
        print("A capital loss only offsets capital gains (this year, the 3 years before, or any year ahead).")
        print(NOT_ADVICE)
    if a.write:
        out = harvest_orders(cands)
        write_json(a.orders, {"generated_by": "trader-tax harvest", "date": today.isoformat(), "orders": out})
        print("Wrote %d proposal%s to %s. Next: /trader-risk, then /trader-broker." % (
            len(out), "" if len(out) == 1 else "s", a.orders))
    return 0


def cmd_wash(a):
    portfolio, pdoc, prices, state, orders, today = _inputs(a)
    fam = family(a.ticker)
    hits = [r for r in loss_sales_to_watch(state, today) if str(r.get("symbol", "")).upper() in fam]
    if a.json:
        print(json.dumps(hits, indent=2))
        return 1 if hits else 0
    if not hits:
        print("No recorded loss sale of %s in the last 30 days: buying it today does not make one "
              "superficial (as far as this log knows)." % "/".join(sorted(fam)))
        print(NOT_ADVICE)
        return 0
    for r in hits:
        print("Buying %s today, in ANY account, would make the %s loss from %s superficial. Wait until %s."
              % ("/".join(sorted(fam)), money(r["gain_cad"]), r["date"], rebuy_date(day(r["date"]))))
    print(NOT_ADVICE)
    return 1


def cmd_fx(a):
    date = a.date or datetime.date.today().isoformat()
    obs_day, rate = boc_rate(date, a.currency)
    print("Bank of Canada %s/CAD on %s: %.4f%s" % (a.currency.upper(), obs_day, rate,
          "" if obs_day == date else " (last business day on or before %s)" % date))
    if a.write:
        doc = read_json(a.prices, "prices", required=False) or {"prices": {}}
        doc.setdefault("fx", {})[a.currency.upper()] = rate
        doc["fx_date"] = obs_day
        write_json(a.prices, doc)
        print("Saved to %s." % a.prices)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="ACB and capital gains, Canadian rules (trader-tax). Not tax advice.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--portfolio", default="trader/portfolio.json")
        p.add_argument("--prices", default="trader/prices.json")
        p.add_argument("--state", default="trader/state/tax.json")
        p.add_argument("--orders", default="trader/orders.json")
        p.add_argument("--today", help="YYYY-MM-DD (default: today)")
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("report", help="ACB, value and unrealized gain per holding")
    common(p)
    p.add_argument("--lots", action="store_true")
    p = sub.add_parser("sell", help="what a sale would realize")
    common(p)
    p.add_argument("ticker")
    p.add_argument("shares", type=float)
    p.add_argument("--price", type=float)
    p.add_argument("--fx", type=float, help="CAD per unit of the stock's currency on the sale date")
    p.add_argument("--fee", type=float, default=0.0, help="commission, in the stock's currency")
    p.add_argument("--account", choices=sorted(ACCOUNTS))
    p.add_argument("--record", action="store_true", help="it filled: update the ACB and log the sale")
    p.add_argument("--method", help=argparse.SUPPRESS)  # US lot methods: ignored, ACB is average cost
    p.add_argument("--lots", help=argparse.SUPPRESS)
    p = sub.add_parser("harvest", help="tax-loss selling candidates")
    common(p)
    p.add_argument("--min-loss", type=float, default=100.0, help="CAD")
    p.add_argument("--min-pct", type=float, default=0.0)
    p.add_argument("--write", action="store_true")
    p = sub.add_parser("wash", help="would buying this today make a recent loss superficial?")
    common(p)
    p.add_argument("ticker")
    p = sub.add_parser("fx", help="Bank of Canada daily rate")
    p.add_argument("--date")
    p.add_argument("--currency", default="USD")
    p.add_argument("--prices", default="trader/prices.json")
    p.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"report": cmd_report, "sell": cmd_sell, "harvest": cmd_harvest, "wash": cmd_wash,
                "fx": cmd_fx}[a.cmd](a)
    except TaxError as e:
        print("lots.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
