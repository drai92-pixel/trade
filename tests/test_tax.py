"""trader-tax: Canadian ACB, CAD conversion, 50% inclusion and superficial losses."""

import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

lots = load("trader-tax", "lots")

PF_PATH = os.path.join(FIX, "trader-tax", "portfolio.json")
PRICES_PATH = os.path.join(FIX, "trader-tax", "prices.json")
TODAY = datetime.date(2026, 10, 13)


def pf():
    with open(PF_PATH) as fh:
        return json.load(fh)


def pdoc():
    with open(PRICES_PATH) as fh:
        return json.load(fh)


def prices():
    return pdoc()["prices"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lots.main(argv)
    return code, out.getvalue(), err.getvalue()


def holding(hs, t, account=None):
    return lots.find(hs, t, account)


class Families(unittest.TestCase):
    def test_interlisted_and_classes(self):
        self.assertEqual(lots.family("RY.TO"), {"RY", "RY.TO"})
        self.assertEqual(lots.family("RY"), {"RY", "RY.TO"})
        self.assertEqual(lots.family("CNI"), {"CNI", "CNR.TO"})
        self.assertEqual(lots.family("GOOG"), {"GOOG", "GOOGL"})
        self.assertEqual(lots.family("T"), {"T"})          # AT&T is not Telus
        self.assertEqual(lots.family("KO"), {"KO"})

    def test_currency_guess(self):
        self.assertEqual(lots.currency_of({"ticker": "RY.TO"}), "CAD")
        self.assertEqual(lots.currency_of({"ticker": "RY"}), "USD")
        self.assertEqual(lots.currency_of({"ticker": "XYZ", "currency": "cad"}), "CAD")


class ACB(unittest.TestCase):
    def test_cost_and_fee_converted_at_trade_date_rate(self):
        hs = lots.holdings(pf())
        self.assertAlmostEqual(holding(hs, "KO")["acb"], (1050 + 1) * 1.44, places=6)
        rows, missing = lots.unrealized(hs, prices(), pdoc())
        ko = [r for r in rows if r["ticker"] == "KO"][0]
        self.assertAlmostEqual(ko["value_cad"], 1261.54, places=2)
        self.assertAlmostEqual(ko["gain_cad"], -251.90, places=2)
        self.assertEqual(missing, [])

    def test_interlisted_listings_share_one_average_cost(self):
        hs = lots.holdings(pf())
        pool = lots.pool_of(hs, "personal", "RY.TO")
        self.assertAlmostEqual(pool["shares"], 15)
        self.assertAlmostEqual(pool["per_share"], (1700 + 600 * 1.40) / 15, places=6)

    def test_registered_has_no_gain(self):
        hs = lots.holdings(pf())
        rows, _ = lots.unrealized(hs, prices(), pdoc())
        axp = [r for r in rows if r["ticker"] == "AXP"][0]
        self.assertIsNone(axp["gain_cad"])
        with self.assertRaises(lots.TaxError):
            lots.simulate_sale(hs, holding(hs, "AXP"), 1, 300, 1.4, 0, TODAY)

    def test_usd_lot_without_fx_is_refused(self):
        p = pf()
        del p["positions"][0]["lots"][0]["fx"]
        with self.assertRaises(lots.TaxError) as e:
            lots.holdings(p)
        self.assertIn("no fx rate", str(e.exception))

    def test_shares_without_lots_are_refused(self):
        with self.assertRaises(lots.TaxError):
            lots.holdings({"positions": [{"ticker": "KO", "shares": 3}]})


class Superficial(unittest.TestCase):
    def test_buy_twelve_days_ago_denies_part_of_the_loss(self):
        hs = lots.holdings(pf())
        s = lots.simulate_sale(hs, holding(hs, "BAC"), 10, 53.96, 1.40, 0, TODAY, [], prices())
        self.assertAlmostEqual(s["gain_cad"], 755.44 - 1014.5 * 10 / 12, places=2)
        self.assertAlmostEqual(s["denied_cad"], round(-s["gain_cad"] * 2 / 10, 2), places=2)
        self.assertIn("added to the ACB of the BAC shares", s["denied_goes"])
        self.assertAlmostEqual(s["taxable_cad"], round(s["allowed_cad"] / 2, 2), places=2)

    def test_selling_everything_keeps_the_whole_loss(self):
        hs = lots.holdings(pf())
        s = lots.simulate_sale(hs, holding(hs, "BAC"), 12, 53.96, 1.40, 0, TODAY, [], prices())
        self.assertLess(s["gain_cad"], 0)
        self.assertEqual(s["denied_cad"], 0.0)

    def test_a_tfsa_rebuy_loses_it_for_good(self):
        p = pf()
        p["positions"].append({"ticker": "KO", "account": "tfsa", "shares": 10,
                               "lots": [{"id": "KO-T1", "date": "2026-10-20", "shares": 10, "cost": 900.0}]})
        hs = lots.holdings(p)
        s = lots.simulate_sale(hs, holding(hs, "KO", "personal"), 10, 90.11, 1.40, 0, TODAY, [], prices())
        self.assertAlmostEqual(s["denied_cad"], -s["gain_cad"], places=2)
        self.assertIn("lost for good", s["denied_goes"])
        self.assertIn("TFSA", s["denied_goes"])

    def test_a_planned_buy_counts(self):
        hs = lots.holdings(pf())
        orders = [{"symbol": "KO", "side": "buy", "qty": "5"}]
        s = lots.simulate_sale(hs, holding(hs, "KO"), 10, 90.11, 1.40, 0, TODAY, orders, prices())
        self.assertAlmostEqual(s["denied_cad"], round(-s["gain_cad"] * 0.5, 2), places=2)

    def test_corp_is_affiliated_but_its_own_pool(self):
        p = pf()
        p["positions"].append({"ticker": "KO", "account": "corp", "shares": 4,
                               "lots": [{"id": "KO-C1", "date": "2026-10-01", "shares": 4, "cost": 360.0,
                                         "fx": 1.37}]})
        hs = lots.holdings(p)
        s = lots.simulate_sale(hs, holding(hs, "KO", "personal"), 10, 90.11, 1.40, 0, TODAY, [], prices())
        self.assertAlmostEqual(s["denied_cad"], round(-s["gain_cad"] * 0.4, 2), places=2)
        self.assertIn("corporation", s["denied_goes"])
        self.assertAlmostEqual(lots.pool_of(hs, "corp", "KO")["per_share"], 360 * 1.37 / 4, places=6)
        self.assertAlmostEqual(s["acb_per_share_cad"], round(1051 * 1.44 / 10, 4), places=4)


class Record(unittest.TestCase):
    def test_record_keeps_average_cost_and_adds_denied_loss(self):
        p, st = pf(), {"realized": []}
        hs = lots.holdings(p)
        h = holding(hs, "BAC")
        s = lots.simulate_sale(hs, h, 10, 53.96, 1.40, 0, TODAY, [], prices())
        lots.record_sale(p, h, s, TODAY, st)
        bac = holding(lots.holdings(p), "BAC")
        self.assertAlmostEqual(bac["shares"], 2, places=6)
        self.assertAlmostEqual(bac["acb"], 1014.5 * 2 / 12 + s["denied_cad"], places=1)
        self.assertEqual(st["realized"][0]["symbol"], "BAC")
        ytd = lots.realized_year(st, 2026)["personal"]
        self.assertAlmostEqual(ytd["net"], s["allowed_cad"], places=2)
        self.assertAlmostEqual(ytd["taxable"], s["allowed_cad"] / 2, places=2)

    def test_full_sale_removes_the_position(self):
        p, st = pf(), {"realized": []}
        hs = lots.holdings(p)
        h = holding(hs, "KO")
        s = lots.simulate_sale(hs, h, 10, 90.11, 1.40, 0, TODAY, [], prices())
        lots.record_sale(p, h, s, TODAY, st)
        self.assertNotIn("KO", [q["ticker"] for q in p["positions"]])
        self.assertAlmostEqual(p["cash"], 500 + 901.1, places=2)

    def test_late_buy_is_flagged(self):
        p, st = pf(), {"realized": []}
        hs = lots.holdings(p)
        h = holding(hs, "KO")
        s = lots.simulate_sale(hs, h, 10, 90.11, 1.40, 0, TODAY, [], prices())
        lots.record_sale(p, h, s, TODAY, st)
        p["positions"].append({"ticker": "KO", "account": "rrsp", "shares": 1,
                               "lots": [{"id": "KO-R1", "date": "2026-10-20", "shares": 1, "cost": 90.0}]})
        late = lots.late_buys(st, lots.holdings(p))
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["buys"][0]["account"], "rrsp")


class Harvest(unittest.TestCase):
    def test_harvest_skips_registered_and_gains(self):
        hs = lots.holdings(pf())
        c = lots.harvest(hs, prices(), pdoc(), TODAY, min_loss=10)
        by = {x["ticker"]: x for x in c}
        self.assertIn("KO", by)
        self.assertIn("BAC", by)
        self.assertEqual(by["BAC"]["denied_cad"], 0.0)   # selling all 12 leaves nothing bought back
        self.assertNotIn("AXP", by)                       # TFSA
        self.assertNotIn("AAPL", by)                      # a gain
        orders = lots.harvest_orders(c)
        self.assertIn("KO", [o["symbol"] for o in orders])
        self.assertIn("superficial loss", orders[0]["why"])

    def test_harvest_does_not_propose_a_superficial_sale(self):
        hs = lots.holdings(pf())
        c = lots.harvest(hs, prices(), pdoc(), TODAY, min_loss=10,
                         orders=[{"symbol": "KO", "side": "buy", "qty": "10"}])
        ko = [x for x in c if x["ticker"] == "KO"][0]
        self.assertGreater(ko["denied_cad"], 0)
        self.assertNotIn("KO", [o["symbol"] for o in lots.harvest_orders(c)])

    def test_cli_report_sell_harvest_wash(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "portfolio.json")
            shutil.copy(PF_PATH, p)
            common = ["--portfolio", p, "--prices", PRICES_PATH, "--state", os.path.join(d, "tax.json"),
                      "--orders", os.path.join(d, "orders.json"), "--today", "2026-10-13"]
            code, out, _ = run(["report", "--lots"] + common)
            self.assertEqual(code, 0)
            self.assertIn("All amounts in CAD", out)
            self.assertIn("registered", out)
            code, out, _ = run(["sell", "KO", "10", "--record"] + common)
            self.assertEqual(code, 0)
            self.assertIn("taxable half", out)
            code, out, _ = run(["wash", "KO"] + common)
            self.assertEqual(code, 1)
            self.assertIn("2026-11-13", out)
            code, out, _ = run(["harvest", "--min-loss", "10", "--write"] + common)
            self.assertEqual(code, 0)
            code, _, err = run(["sell", "XYZ", "1"] + common)
            self.assertEqual(code, 1)
        finally:
            shutil.rmtree(d)

    def test_missing_portfolio(self):
        code, _, err = run(["report", "--portfolio", "/nope.json"])
        self.assertEqual(code, 1)
        self.assertIn("no portfolio", err)


class BankOfCanada(unittest.TestCase):
    def test_rate_falls_back_to_last_business_day(self):
        doc = {"observations": [{"d": "2026-10-09", "FXUSDCAD": {"v": "1.3801"}},
                                {"d": "2026-10-10", "FXUSDCAD": {"v": "1.3822"}}]}

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        d, r = lots.boc_rate("2026-10-12", "USD", opener=lambda url, timeout: R(json.dumps(doc).encode()))
        self.assertEqual(d, "2026-10-10")
        self.assertAlmostEqual(r, 1.3822)


if __name__ == "__main__":
    unittest.main()
