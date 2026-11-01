import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone

import dashboard
import email_report
import flex_export
import market_metrics
import one_reader
import recon_one_os
import reconcile
import snapshots


def option(account="GroupA", *, qty=1.0, price=10.0):
    return {
        "account": account,
        "secType": "OPT",
        "underlying": "SPX",
        "tradingClass": "SPXW",
        "expiry": "20260807",
        "strike": 7400.0,
        "right": "P",
        "qty": qty,
        "avg_price": price,
    }


def one_leg(account="ALT", **over):
    """A raw ONE report leg, as read_summary_report() emits it."""
    return {
        "account": account, "trade_id": "1", "underlying": "SPX",
        "tradingClass": "SPXW", "expiry": "20260808", "expiry_listed": "20260807",
        "strike": 7400.0, "right": "P", "multiplier": 100.0, "qty": -1.0,
        "open_price": 10.0, "is_open": True, "is_expired": False,
        "unsettled_in_one": False, "trade_name": None,
        **over,
    }


def trade_row(account="ALT", *, trade_id="1", name="A Trade",
              margin=1000.0, status="Open"):
    """A TRADE summary row, as parse_trade_row() emits it."""
    return {
        "account": account, "trade_id": trade_id, "trade_name": name,
        "status": status, "underlying": "SPX", "expiration": "20260807",
        "open_date": "8/08/2026 6:11 AM", "margin": margin,
        "has_margin_column": True,
    }


def config():
    return {
        "account_map": {"GroupA": "U1"},
        "ignore_one_accounts": [],
        "one_export_dirs": [r"Z:\tws-matcher-test-no-files"],
        "tolerances": {
            "price_abs": 0.05,
            "price_pct": 0.005,
            "expiry_days": 1,
        },
        "email": {
            "enabled": True,
            "username": "sender@example.com",
            "password": "test-password",
            "to_email": "recipient@example.com",
        },
    }


class FlexExportHousekeepingTests(unittest.TestCase):
    def _row(self):
        return ["Trades", "U1", "SPX", "OPT", "SPXW  260904P07400000"]

    def test_superseded_export_is_deleted_not_left_beside_the_current_one(self):
        with tempfile.TemporaryDirectory() as d:
            # a COMPLETE run replaces a previously quarantined file
            blocked = os.path.join(
                d, f"ONEImport_U1_{flex_export.POSSIBLY_INCOMPLETE}.csv")
            open(blocked, "w").close()
            flex_export.save_account_files(
                {"U1": [self._row()]},
                {"U1": {"status": flex_export.COMPLETE}}, out_dir=d)
            self.assertEqual(sorted(os.listdir(d)), ["ONEImport_U1.csv"])

    def test_incomplete_run_removes_the_stale_complete_file(self):
        with tempfile.TemporaryDirectory() as d:
            normal = os.path.join(d, "ONEImport_U1.csv")
            open(normal, "w").close()
            flex_export.save_account_files(
                {"U1": [self._row()]},
                {"U1": {"status": flex_export.POSSIBLY_INCOMPLETE}}, out_dir=d)
            self.assertEqual(
                sorted(os.listdir(d)),
                [f"ONEImport_U1_{flex_export.POSSIBLY_INCOMPLETE}.csv"])

    def test_existing_dot_stale_leftovers_are_swept(self):
        """Folders from the rename era heal themselves on the next save."""
        with tempfile.TemporaryDirectory() as d:
            for name in ("ONEImport_U1.csv.stale",
                         "ONEImport_U9_POSSIBLY_INCOMPLETE.csv.stale"):
                open(os.path.join(d, name), "w").close()
            keep = os.path.join(d, "notes.txt")
            open(keep, "w").close()
            flex_export.save_account_files(
                {"U1": [self._row()]},
                {"U1": {"status": flex_export.COMPLETE}}, out_dir=d)
            self.assertEqual(sorted(os.listdir(d)),
                             ["ONEImport_U1.csv", "notes.txt"])


class ExpiryAndAssignmentRadarTests(unittest.TestCase):
    @staticmethod
    def contract(**kw):
        base = {"secType": "OPT", "symbol": "MSFT", "tradingClass": "MSFT",
                "lastTradeDateOrContractMonth": "20260821", "strike": 390.0,
                "right": "C", "conId": 1, "multiplier": "100"}
        base.update(kw)
        return type("C", (), base)()

    @staticmethod
    def position(contract, qty, account="U1"):
        return type("P", (), {"account": account, "contract": contract,
                              "position": qty})()

    def test_short_itm_option_with_no_extrinsic_is_assignment_risk(self):
        """The MSFT early assignment, as it looked the day before it happened."""
        c = self.contract()
        greeks = {1: {"und_price": 481.5, "opt_price": 91.6, "delta": 0.99}}
        r = market_metrics.assess_expiry_risk(
            [self.position(c, -1.0)], greeks, as_of=date(2026, 8, 19))
        [hit] = r["assignment_risk"]
        self.assertEqual(hit["underlying"], "MSFT")
        self.assertAlmostEqual(hit["extrinsic"], 0.10, 2)

    def test_cash_settled_index_is_never_assignment_risk(self):
        """SPX is European and cash settled: deep ITM, still cannot be assigned."""
        c = self.contract(symbol="SPX", tradingClass="SPXW", strike=7000.0,
                          right="P", conId=2)
        greeks = {2: {"und_price": 6800.0, "opt_price": 201.0, "delta": -0.98}}
        r = market_metrics.assess_expiry_risk(
            [self.position(c, -4.0)], greeks, as_of=date(2026, 8, 19))
        self.assertEqual(r["assignment_risk"], [])
        self.assertTrue(r["expiring"][0]["cash_settled"])

    def test_long_option_is_not_assignment_risk(self):
        c = self.contract()
        greeks = {1: {"und_price": 481.5, "opt_price": 91.6, "delta": 0.99}}
        r = market_metrics.assess_expiry_risk(
            [self.position(c, +1.0)], greeks, as_of=date(2026, 8, 19))
        self.assertEqual(r["assignment_risk"], [])

    def test_short_itm_option_still_holding_extrinsic_is_not_flagged(self):
        c = self.contract()
        greeks = {1: {"und_price": 481.5, "opt_price": 99.0, "delta": 0.80}}
        r = market_metrics.assess_expiry_risk(
            [self.position(c, -1.0)], greeks, as_of=date(2026, 8, 19))
        self.assertEqual(r["assignment_risk"], [])

    def test_theta_is_recomputed_because_ib_reports_it_wrong(self):
        """IB gave +4.36/day on a near-ATM long call; decay must be negative."""
        t = market_metrics.bs_theta_per_day(7720.4, 7800.0, 0.173, 0.1287, "C")
        self.assertLess(t, 0)
        self.assertLess(abs(t), 3)          # ~V/(2T), not IB's 4-6/day


class SnapshotFreshnessTests(unittest.TestCase):
    """The on-disk snapshots are a moment frozen in a file. Every CLI entry
    point reads them, and flex_export turns the IBKR one into a ONE import, so
    an old file has to be refused rather than reported on."""

    def path(self, name="canonical_positions.json"):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        return os.path.join(d, name)

    @staticmethod
    def snap(minutes_old):
        stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
        return {"captured_at": stamp.isoformat(), "legs": [{"x": 1}]}

    def write(self, path, data):
        snapshots.write_json_atomic(path, data)
        return path

    def test_fresh_snapshot_loads(self):
        p = self.write(self.path(), self.snap(2))
        self.assertEqual(len(snapshots.load_snapshot(p)["legs"]), 1)

    def test_stale_snapshot_is_refused_and_says_how_old(self):
        p = self.write(self.path(), self.snap(60 * 24 * 76))
        with self.assertRaises(snapshots.StaleSnapshotError) as cm:
            snapshots.load_snapshot(p)
        msg = str(cm.exception)
        self.assertIn("76.0 days old", msg)
        self.assertIn("--stale-ok", msg)

    def test_stale_snapshot_loads_when_explicitly_allowed(self):
        p = self.write(self.path(), self.snap(60 * 24 * 76))
        self.assertEqual(len(snapshots.load_snapshot(p, allow_stale=True)["legs"]), 1)

    def test_snapshot_without_a_timestamp_fails_closed(self):
        """Unable to prove it is fresh is not the same as fresh."""
        p = self.write(self.path(), {"legs": []})
        with self.assertRaises(snapshots.StaleSnapshotError):
            snapshots.load_snapshot(p)

    def test_unparseable_timestamp_fails_closed(self):
        p = self.write(self.path(), {"captured_at": "last Tuesday", "legs": []})
        with self.assertRaises(snapshots.StaleSnapshotError):
            snapshots.load_snapshot(p)

    def test_naive_timestamp_is_read_as_utc(self):
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=3)).replace(tzinfo=None)
        p = self.write(self.path(), {"captured_at": stamp.isoformat(), "legs": []})
        self.assertIsNotNone(snapshots.load_snapshot(p))

    def test_missing_file_returns_the_default_when_one_is_offered(self):
        p = os.path.join(tempfile.mkdtemp(), "nope.json")
        self.assertEqual(snapshots.load_snapshot(p, default={"legs": []}), {"legs": []})

    def test_missing_file_raises_when_no_default(self):
        p = os.path.join(tempfile.mkdtemp(), "nope.json")
        with self.assertRaises(snapshots.StaleSnapshotError):
            snapshots.load_snapshot(p)

    def test_atomic_write_replaces_content_and_leaves_no_temp_files(self):
        p = self.write(self.path(), {"captured_at": "x", "v": 1})
        snapshots.write_json_atomic(p, {"captured_at": "y", "v": 2})
        with open(p) as fh:
            self.assertEqual(json.load(fh)["v"], 2)
        leftovers = [f for f in os.listdir(os.path.dirname(p)) if f.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_atomic_write_serialises_datetimes_like_the_old_writer(self):
        p = self.path()
        snapshots.write_json_atomic(p, {"captured_at": datetime(2026, 9, 8)})
        with open(p) as fh:
            self.assertIn("2026-09-08", json.load(fh)["captured_at"])

    def test_wants_stale_reads_the_flag(self):
        self.assertTrue(snapshots.wants_stale(["reconcile.py", "--stale-ok"]))
        self.assertFalse(snapshots.wants_stale(["reconcile.py"]))

    def test_every_cli_entry_point_agrees_on_the_snapshot_paths(self):
        """The daemon writes these paths; the CLI tools must read the same ones
        or the freshness guarantee silently applies to the wrong file."""
        self.assertEqual(flex_export.IBKR_JSON, reconcile.IBKR_JSON)
        self.assertEqual(email_report.IBKR_JSON, reconcile.IBKR_JSON)
        self.assertEqual(email_report.ONE_JSON, reconcile.ONE_JSON)


class ExpirySettlementTests(unittest.TestCase):
    """Pricing an expired-but-unsettled ONE trade against the print that
    actually settled it, instead of assuming the legs expired worthless."""

    RUT_AUG31 = {("RUT", "20260831"): {
        "open": 2967.43, "high": 2967.43, "low": 2944.34, "close": 2956.45,
        "bar_date": "20260831", "provisional": False}}

    @staticmethod
    def leg(strike, qty, price, right="P", klass="RUTW"):
        return {"tradingClass": klass, "expiry": "20260831", "strike": strike,
                "right": right, "qty": qty, "open_price": price,
                "multiplier": 100.0}

    @staticmethod
    def trade(legs, underlying="RUT", expiry="20260831"):
        pnl = round(sum(-l["qty"] * l["open_price"] * l["multiplier"]
                        for l in legs), 2)
        return {"account": "ALT", "trade_id": "155", "trade_name": "31 AUG",
                "underlying": underlying, "expiry": expiry,
                "pnl_if_worthless": pnl, "legs": legs}

    def condor(self):
        return self.trade([self.leg(2995.0, 2, 8.41),
                           self.leg(3000.0, -2, 28.805),
                           self.leg(3005.0, -2, 11.35),
                           self.leg(3010.0, 2, 21.39)])

    def test_deep_itm_legs_settle_at_intrinsic_not_zero(self):
        """The 31 Aug RUT trade: RUT printed 2956.45, below all four strikes,
        so every leg was cash settled at intrinsic. Zero would be the wrong
        number to type into ONE even though a balanced condor's intrinsics
        cancel and the trade P&L happens to land in the same place."""
        [t] = one_reader.apply_settlement([self.condor()], self.RUT_AUG31)
        self.assertEqual([l["settle_price"] for l in t["legs"]],
                         [38.55, 43.55, 48.55, 53.55])
        self.assertTrue(all(l["itm"] for l in t["legs"]))
        self.assertEqual(t["net_settlement"], 0.0)
        self.assertAlmostEqual(t["pnl_settled"], t["pnl_if_worthless"], 2)
        self.assertEqual(t["settlement"]["basis"], "close")
        self.assertFalse(t["settlement"]["approximate"])
        self.assertTrue(t["settlement"]["cash_settled"])

    def test_print_inside_the_structure_moves_the_realised_pnl(self):
        """Where assuming worthless actually costs money: settle at 3002 and
        the long 3010 wing alone is worth 8 points the trade never books."""
        closes = {("RUT", "20260831"): {"close": 3002.0, "open": 3002.0,
                                        "bar_date": "20260831",
                                        "provisional": False}}
        [t] = one_reader.apply_settlement([self.condor()], closes)
        self.assertEqual([l["settle_price"] for l in t["legs"]],
                         [0.0, 0.0, 3.0, 8.0])
        self.assertEqual(t["net_settlement"], 1000.0)
        self.assertAlmostEqual(t["pnl_settled"] - t["pnl_if_worthless"],
                               1000.0, 2)

    def test_all_out_of_the_money_agrees_with_the_worthless_case(self):
        closes = {("RUT", "20260831"): {"close": 3100.0, "open": 3100.0,
                                        "bar_date": "20260831",
                                        "provisional": False}}
        [t] = one_reader.apply_settlement([self.condor()], closes)
        self.assertEqual({l["settle_price"] for l in t["legs"]}, {0.0})
        self.assertEqual(t["settlement"]["itm_legs"], 0)
        self.assertAlmostEqual(t["pnl_settled"], t["pnl_if_worthless"], 2)

    def test_am_settled_monthly_prices_off_the_open_and_says_so(self):
        """A 3rd-Friday SPX monthly settles on SET, built from the opening
        prints. The index open is the closest proxy IBKR has; the close is
        simply the wrong number, so the basis has to follow the trading class."""
        t = self.trade([{"tradingClass": "SPX", "expiry": "20260821",
                         "strike": 6550.0, "right": "P", "qty": -1.0,
                         "open_price": 20.0, "multiplier": 100.0}],
                       underlying="SPX", expiry="20260821")
        closes = {("SPX", "20260821"): {"open": 6500.0, "close": 6600.0,
                                        "bar_date": "20260821",
                                        "provisional": False}}
        [t] = one_reader.apply_settlement([t], closes)
        self.assertEqual(t["legs"][0]["settle_price"], 50.0)
        self.assertEqual(t["settlement"]["basis"], "open")
        self.assertTrue(t["settlement"]["approximate"])
        self.assertIn("SET/RLS", one_reader.settlement_note(t))

    def test_physically_settled_itm_leg_warns_about_shares(self):
        t = self.trade([{"tradingClass": "IWM", "expiry": "20260821",
                         "strike": 240.0, "right": "P", "qty": -3.0,
                         "open_price": 2.0, "multiplier": 100.0}],
                       underlying="IWM", expiry="20260821")
        closes = {("IWM", "20260821"): {"open": 235.0, "close": 236.0,
                                        "bar_date": "20260821",
                                        "provisional": False}}
        [t] = one_reader.apply_settlement([t], closes)
        self.assertEqual(t["legs"][0]["settle_price"], 4.0)
        self.assertFalse(t["settlement"]["cash_settled"])
        self.assertTrue(t["settlement"]["assigned_shares"])

    def test_missing_print_leaves_the_worthless_case_intact(self):
        [t] = one_reader.apply_settlement([self.condor()], {})
        self.assertNotIn("settlement", t)
        self.assertIsNone(t["legs"][0].get("settle_price"))
        self.assertEqual(one_reader.settled_pnl(t), t["pnl_if_worthless"])

    def test_settlement_basis_follows_the_trading_class(self):
        self.assertEqual(market_metrics.settlement_basis("RUTW", "RUT"), "close")
        self.assertEqual(market_metrics.settlement_basis("SPXW", "SPX"), "close")
        self.assertEqual(market_metrics.settlement_basis("SPX", "SPX"), "open")
        self.assertEqual(market_metrics.settlement_basis(None, "RUT"), "open")


class SettlementPrintFetchTests(unittest.TestCase):
    """collect_settlement_closes against a stubbed TWS."""

    class Bar:
        def __init__(self, d, o, c):
            self.date, self.open, self.close = d, o, c
            self.high, self.low = max(o, c), min(o, c)

    class StubIB:
        def __init__(self, bars):
            self.bars, self.requests = bars, 0

        def reqContractDetails(self, contract):
            return [type("D", (), {"contract": contract})()]

        def reqHistoricalData(self, *a, **kw):
            self.requests += 1
            return self.bars

    def setUp(self):
        market_metrics._provisional.clear()

    def bars(self):
        return [self.Bar(date(2026, 8, 28), 3014.15, 2972.37),
                self.Bar(date(2026, 8, 31), 2967.43, 2956.45)]

    def cache_path(self):
        fh = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        fh.close()
        os.unlink(fh.name)
        self.addCleanup(lambda: os.path.exists(fh.name) and os.unlink(fh.name))
        return fh.name

    def test_fetches_the_expiry_bar_and_caches_it(self):
        ib = self.StubIB(self.bars())
        path = self.cache_path()
        got = market_metrics.collect_settlement_closes(
            ib, [("RUT", "20260831")], cache_path=path)
        self.assertAlmostEqual(got[("RUT", "20260831")]["close"], 2956.45, 2)
        self.assertFalse(got[("RUT", "20260831")]["provisional"])
        # a past session's print never changes: second call must not hit TWS
        again = market_metrics.collect_settlement_closes(
            ib, [("RUT", "20260831")], cache_path=path)
        self.assertEqual(ib.requests, 1)
        self.assertEqual(again[("RUT", "20260831")]["close"],
                         got[("RUT", "20260831")]["close"])

    def test_saturday_osi_date_falls_back_to_the_last_session(self):
        """An AM-settled monthly encodes the Saturday; the print is Friday's."""
        ib = self.StubIB(self.bars())
        got = market_metrics.collect_settlement_closes(
            ib, [("RUT", "20260901")], cache_path=self.cache_path())
        self.assertEqual(got[("RUT", "20260901")]["bar_date"], "20260831")

    def test_todays_print_is_provisional_and_never_written_to_disk(self):
        """A same-day expiry settles at 4pm; until then the "close" is just the
        last trade, so it is held for a few minutes and re-asked after that --
        but it must never reach the on-disk cache, which is treated as final."""
        today = date.today()
        ib = self.StubIB([self.Bar(today, 100.0, 101.0)])
        path = self.cache_path()
        stamp = today.strftime("%Y%m%d")
        got = market_metrics.collect_settlement_closes(
            ib, [("IWM", stamp)], cache_path=path)
        self.assertTrue(got[("IWM", stamp)]["provisional"])
        self.assertFalse(os.path.exists(path))    # nothing final to persist

        # inside the hold window the daemon's next cycle costs no TWS request
        market_metrics.collect_settlement_closes(ib, [("IWM", stamp)],
                                                 cache_path=path)
        self.assertEqual(ib.requests, 1)

        market_metrics._provisional.clear()        # window elapsed
        market_metrics.collect_settlement_closes(ib, [("IWM", stamp)],
                                                 cache_path=path)
        self.assertEqual(ib.requests, 2)

    def test_a_broken_symbol_is_absent_not_fatal(self):
        class Angry(self.StubIB):
            def reqHistoricalData(self, *a, **kw):
                raise RuntimeError("no market data permissions")

        got = market_metrics.collect_settlement_closes(
            Angry([]), [("RUT", "20260831")], cache_path=self.cache_path())
        self.assertEqual(got, {})


class ReconciliationTests(unittest.TestCase):
    def test_one_expired_leg_is_not_treated_as_open(self):
        self.assertTrue(one_reader.is_expired("20260801", date(2026, 8, 4)))
        self.assertFalse(one_reader.is_expired("20260804", date(2026, 8, 4)))
        self.assertFalse(one_reader.is_expired("20260805", date(2026, 8, 4)))

        row = [
            "", "", "GroupA", "100", "", "Sell", "4",
            "SPXW  260801P07250000", "31/07/2026", "Put", "",
            "SPX", "10.0", "", "",
        ]
        leg = one_reader.parse_leg_row(row)
        self.assertTrue(leg["is_expired"])
        self.assertFalse(leg["is_open"])
        self.assertEqual(one_reader.net_positions([leg]), [])

    def test_one_report_order_prefix_is_not_part_of_account_name(self):
        self.assertEqual(
            one_reader.normalize_account_name("  12.GroupA "), "GroupA")
        row = [
            "", "", "1.GroupA", "100", "", "Buy", "1",
            "SPXW  260808P07400000", "7/08/2026", "Put", "",
            "SPX", "10.0", "", "",
        ]
        self.assertEqual(one_reader.parse_leg_row(row)["account"], "GroupA")

    def test_numbered_one_account_matches_unprefixed_config(self):
        ibkr = {"captured_at": "now", "legs": [
            {**option(account="U1")}], "fills_today": []}
        one = {"source_file": "report.csv", "positions": [
            {**option(account="1.GroupA"), "avg_price": 10.0}]}
        result = reconcile.reconcile_snapshots(ibkr, one, config())
        finding = result["accounts"]["U1"][0]
        self.assertEqual(finding["status"], "MATCH")
        self.assertEqual(result["unmapped_one_groups"], {})

    def test_stock_only_is_expected_and_not_actionable(self):
        stock = {
            "account": "U1", "secType": "STRAT11", "underlying": "EVN",
            "tradingClass": "EVN", "expiry": None, "strike": 0.0,
            "right": "", "qty": 250.0, "avg_price": 12.8,
        }
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [stock], "fills_today": []},
            {"source_file": "report.csv", "positions": []},
            config(),
        )
        finding = result["accounts"]["U1"][0]
        self.assertTrue(finding["expected"])
        self.assertFalse(reconcile.is_actionable_finding(finding))

    def test_aged_price_difference_is_fifo_information(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [
                {**option(account="U1"), "avg_price": 12.0}],
             "fills_today": []},
            {"source_file": "report.csv", "positions": [
                {**option(account="GroupA"), "avg_price": 10.0}]},
            config(),
        )
        finding = result["accounts"]["U1"][0]
        self.assertEqual(finding["status"], "MATCH_FIFO_AVG")
        self.assertFalse(reconcile.is_actionable_finding(finding))

    def test_exact_current_fill_price_difference_is_actionable(self):
        fill = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260807",
            "strike": 7400.0, "right": "P", "shares": 1.0,
            "side": "BOT", "price": 9.0,
        }
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [
                {**option(account="U1"), "avg_price": 9.1}],
             "fills_today": [fill]},
            {"source_file": "report.csv", "positions": [
                {**option(account="GroupA"), "avg_price": 10.0}]},
            config(),
        )
        finding = result["accounts"]["U1"][0]
        self.assertEqual(finding["status"], "PRICE_DRIFT")
        self.assertTrue(reconcile.is_actionable_finding(finding))

    def test_unmapped_group_is_retained_as_an_assurance_failure(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "legs": [], "fills_today": []},
            {"source_file": "report.csv", "positions": [
                {**option(account="UnknownBook"), "avg_price": 10.0}]},
            config(),
        )
        self.assertEqual(result["unmapped_one_groups"], {"UnknownBook": 1})

    def test_intraday_closes_do_not_distort_remaining_entry_price(self):
        # Includes the reported six sells / five buybacks regression, plus
        # scale-ins, flat/reopen and reversals on both sides of the market.
        cases = [
            ([-6, 4, 1], [54.48, 52.62, 52.42], 54.48),
            ([6, -4, -1], [54.48, 52.62, 52.42], 54.48),
            ([2, 2, -3], [10, 20, 30], 15),
            ([2, -1, 2], [10, 30, 25], 20),
            ([-2, 2, -1], [10, 8, 20], 20),
            ([2, -3], [10, 20], 20),
            ([-2, 3], [10, 20], 20),
        ]
        for quantities, prices, expected in cases:
            for wrong_one_price in (False, True):
                with self.subTest(quantities=quantities, wrong=wrong_one_price):
                    fills = [
                        {**option(account="U1"), "shares": abs(q),
                         "side": "BOT" if q > 0 else "SLD", "price": px,
                         "time": f"2026-08-07 14:00:0{i}+00:00"}
                        for i, (q, px) in enumerate(zip(quantities, prices))
                    ]
                    qty = sum(quantities)
                    result = reconcile.reconcile_snapshots(
                        {"legs": [option(account="U1", qty=qty,
                                         price=expected - 0.0135)],
                         "fills_today": list(reversed(fills))},
                        {"positions": [option(qty=qty, price=expected +
                                              (1 if wrong_one_price else 0))]},
                        config(),
                    )
                    [finding] = result["accounts"]["U1"]
                    self.assertEqual(finding["ibkr_px"], expected)
                    self.assertEqual(finding["status"],
                                     "PRICE_DRIFT" if wrong_one_price else "MATCH")
                    if not wrong_one_price:
                        self.assertEqual(finding["pnl_divergence"], 0)

    def test_ambiguous_mixed_fills_keep_broker_cost(self):
        for times in [(None, None), ("bad", "bad"),
                      ("2026-08-07 14:00:00+00:00",) * 2]:
            with self.subTest(times=times):
                fills = [
                    {**option(account="U1"), "shares": abs(q),
                     "side": "BOT" if q > 0 else "SLD", "price": px,
                     "time": timestamp}
                    for q, px, timestamp in zip([-2, 1], [10, 8], times)
                ]
                [bucket] = reconcile.net_ibkr(
                    [option(account="U1", qty=-1, price=9.99)],
                    reconcile.build_fill_price_map(fills))
                self.assertEqual(bucket["avg_price"], 9.99)
                self.assertNotIn("price_source", bucket)

    def test_partial_session_fills_do_not_replace_aged_entry_price(self):
        fills = [{**option(account="U1"), "shares": 1,
                  "side": "SLD", "price": 20}]
        [bucket] = reconcile.net_ibkr(
            [option(account="U1", qty=-3, price=15)],
            reconcile.build_fill_price_map(fills))
        self.assertEqual(bucket["avg_price"], 15)
        self.assertNotIn("price_source", bucket)

    def test_same_side_partial_fills_have_weighted_entry_price(self):
        fills = [{**option(account="U1"), "shares": q,
                  "side": "BOT", "price": px}
                 for q, px in [(1, 10), (3, 20)]]
        [bucket] = reconcile.net_ibkr(
            [option(account="U1", qty=4)],
            reconcile.build_fill_price_map(fills))
        self.assertEqual(bucket["avg_price"], 17.5)
        self.assertEqual(bucket["price_source"], "fill")

    def test_one_legs_are_netted_on_the_listed_expiry_not_the_osi_symbol(self):
        """ONE's OSI symbol runs a day late; the Expiry column is the truth."""
        leg = {
            "account": "GroupA", "underlying": "SPX", "tradingClass": "SPXW",
            "expiry": "20260822", "expiry_listed": "20260821",
            "strike": 7400.0, "right": "P", "multiplier": 100.0,
            "qty": -1.0, "open_price": 10.0, "is_open": True, "trade_id": "1",
        }
        [pos] = one_reader.net_positions([leg])
        self.assertEqual(pos["expiry"], "20260821")
        self.assertEqual(pos["expiry_osi"], "20260822")

    def test_am_monthly_still_pairs_within_the_one_day_window(self):
        """IBKR reports the Thursday last-trade date, ONE the Friday expiry."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=-1.0), "expiry": "20260917"}]},
            {"source_file": "report.csv", "positions": [
                {**option(qty=-1.0), "expiry": "20260918",
                 "avg_price": 10.0}]},
            config(),
        )
        [finding] = result["accounts"]["U1"]
        self.assertEqual(finding["status"], "MATCH")
        self.assertEqual(finding["expiry_offset_days"], 1)

    def test_adjacent_weekly_expiries_do_not_cross_pair(self):
        """A 1-day window must not marry a Wednesday leg to a Friday one."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=-1.0), "expiry": "20260819"}]},
            {"source_file": "report.csv", "positions": [
                {**option(qty=-1.0), "expiry": "20260821",
                 "avg_price": 10.0}]},
            config(),
        )
        self.assertEqual(
            sorted(f["status"] for f in result["accounts"]["U1"]),
            ["IBKR_ONLY", "ONE_ONLY"],
        )

    def test_expired_leg_still_open_in_one_is_reported_not_suppressed(self):
        """The regression that hid a real assignment: at expiry IBKR drops the
        contract and the reader stops counting it, so neither side can raise a
        finding. ONE's P&L stays wrong until the trade is settled."""
        legs = [
            one_leg(trade_id="104", strike=380.0, qty=2.0, open_price=14.56,
                    expiry_listed="20260821", unsettled_in_one=True,
                    is_open=False, is_expired=True, trade_name="MSFT"),
            one_leg(trade_id="104", strike=390.0, qty=-2.0, open_price=18.93,
                    expiry_listed="20260821", unsettled_in_one=True,
                    is_open=False, is_expired=True, trade_name="MSFT"),
        ]
        self.assertEqual(one_reader.net_positions(legs), [])   # not a position
        [t] = one_reader.find_unsettled_trades(legs)
        self.assertEqual(t["trade_id"], "104")
        self.assertEqual(t["expiry"], "20260821")
        # long 2 @14.56 costs 2912; short 2 @18.93 keeps 3786
        self.assertAlmostEqual(t["pnl_if_worthless"], 874.0, places=2)

    def test_leg_closed_in_one_before_expiry_is_not_unsettled(self):
        settled = one_leg(is_open=False, is_expired=True,
                          unsettled_in_one=False)
        live = one_leg(is_open=True, is_expired=False)
        self.assertEqual(one_reader.find_unsettled_trades([settled, live]), [])

    def test_close_date_clears_the_unsettled_flag(self):
        row = ["", "", "ALT", "104", "", "Sell", "2",
               "MSFT  260801P00390000", "31/07/2026", "Put", "",
               "MSFT", "18.93", "", ""]
        expired = one_reader.parse_leg_row(row)
        self.assertTrue(expired["unsettled_in_one"])
        row[14] = "22/08/2026 5:00:00 AM"          # CloseDate present
        self.assertFalse(one_reader.parse_leg_row(row)["unsettled_in_one"])

    def test_unsettled_expiry_breaks_the_all_clear(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": []},
            {"source_file": "report.csv", "positions": [],
             "unsettled_trades": [{
                 "account": "GroupA", "trade_id": "104",
                 "trade_name": "MSFT", "underlying": "MSFT",
                 "expiry": "20260821", "pnl_if_worthless": 874.0,
                 "legs": [{"tradingClass": "MSFT", "expiry": "20260821",
                           "strike": 390.0, "right": "P", "qty": -2.0,
                           "open_price": 18.93}],
             }]},
            config(),
        )
        self.assertEqual(result["accounts"], {})       # nothing reconcilable
        [t] = result["unsettled_trades"]
        self.assertEqual(t["ibkr_account"], "U1")

    def test_cost_basis_gap_is_priced_in_dollars(self):
        """ONE reports no P&L for an open leg, so the divergence is the signal."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=-4.0), "avg_price": 25.71,
                 "multiplier": 100.0}]},
            {"source_file": "r.csv", "positions": [
                {**option(qty=-4.0), "avg_price": 25.00}]},
            config(),
        )
        [f] = result["accounts"]["U1"]
        # IBKR cost 25.71 vs ONE 25.00 on -4 lots: ONE will overstate by 284
        self.assertAlmostEqual(f["pnl_divergence"], (25.71 - 25.00) * -4 * 100, 2)
        summary = result["pnl_divergence"]
        self.assertAlmostEqual(summary["net"], f["pnl_divergence"], 2)
        self.assertAlmostEqual(summary["gross"], abs(f["pnl_divergence"]), 2)
        self.assertEqual(summary["by_account"]["U1"], f["pnl_divergence"])

    def test_large_cost_basis_gap_is_still_only_a_lot_convention(self):
        """Regression: a big gap here is a long scaling history, not an error.

        Rebuilt from the real fills behind RUT 260919P2950 (ONE trade 142), where
        the leg was scaled into over three weeks and partially closed without ever
        going flat.  ONE's moving average lands on 46.04667 and IBKR's FIFO on
        26.3958 -- both arithmetically exact.  An earlier version escalated this
        to an actionable flag and told the user to retype ONE's price, which would
        have corrupted a correct book.
        """
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=4.0), "avg_price": 26.3958}]},
            {"source_file": "r.csv", "positions": [
                {**option(qty=4.0), "avg_price": 46.04667}]},
            config(),
        )
        [f] = result["accounts"]["U1"]
        self.assertEqual(f["status"], "MATCH_FIFO_AVG")
        self.assertFalse(reconcile.is_actionable_finding(f))
        # the dollars are still reported, as a reporting difference
        self.assertAlmostEqual(f["pnl_divergence"],
                               (26.3958 - 46.04667) * 4 * 100, delta=0.05)

    def test_moving_average_and_fifo_reproduce_both_reported_prices(self):
        """The two conventions, run over one real fill sequence, land where the
        two systems say they land -- which is why neither needs correcting."""
        fills = [(+2, 68.61, 2.70), (+2, 54.85, 2.70), (-2, 76.90, 2.70),
                 (+1, 39.51, 1.00), (+1, 39.51, 1.70), (+2, 27.43, 2.70)]
        qty = cost = 0.0
        for q, px, _c in fills:                      # ONE: moving average
            if qty == 0 or (qty > 0) == (q > 0):
                cost = (cost * abs(qty) + px * abs(q)) / (abs(qty) + abs(q))
            qty += q
        self.assertAlmostEqual(cost, 42.89, 5)       # ONE reports 42.89

        lots = []                                    # IBKR: FIFO incl commission
        for q, px, c in fills:
            unit = px + (c / abs(q) / 100.0) * (1 if q > 0 else -1)
            while lots and (lots[0][0] > 0) != (q > 0) and q:
                take = min(abs(lots[0][0]), abs(q))
                lots[0][0] -= take * (1 if lots[0][0] > 0 else -1)
                q -= take * (1 if q > 0 else -1)
                if lots[0][0] == 0:
                    lots.pop(0)
            if q:
                lots.append([q, unit])
        held = sum(l[0] for l in lots)
        self.assertEqual(held, 6)
        self.assertAlmostEqual(sum(l[0] * l[1] for l in lots) / held, 40.6102, 4)

    def test_small_cost_basis_gap_stays_a_fifo_note(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=1.0), "avg_price": 10.30}]},
            {"source_file": "r.csv", "positions": [
                {**option(qty=1.0), "avg_price": 10.0}]},
            config(),
        )
        [f] = result["accounts"]["U1"]
        self.assertEqual(f["status"], "MATCH_FIFO_AVG")
        self.assertFalse(reconcile.is_actionable_finding(f))

    def test_offsetting_divergences_net_out_but_gross_still_reports(self):
        """A small net can hide two large, opposite per-leg disagreements."""
        summary = reconcile.summarise_pnl_divergence({"U1": [
            {"pnl_divergence": 5000.0, "label": "SPX 7000P", "underlying": "SPX"},
            {"pnl_divergence": -4900.0, "label": "SPX 7100P", "underlying": "SPX"},
        ]})
        self.assertAlmostEqual(summary["net"], 100.0, 2)
        self.assertAlmostEqual(summary["gross"], 9900.0, 2)
        self.assertEqual(len(summary["worst"]), 2)

    def test_quantity_mismatch_has_no_meaningful_divergence(self):
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [], "legs": [
                {**option(account="U1", qty=-4.0), "avg_price": 25.71}]},
            {"source_file": "r.csv", "positions": [
                {**option(qty=-2.0), "avg_price": 25.00}]},
            config(),
        )
        [f] = result["accounts"]["U1"]
        self.assertEqual(f["status"], "QTY_MISMATCH")
        self.assertIsNone(f["pnl_divergence"])

    def test_ghost_trade_is_detected_from_zero_risk(self):
        legs = [{**one_leg(), "trade_id": "154"}]
        trades = [trade_row(trade_id="154", name="", margin=0.0)]
        [ghost] = one_reader.find_ghost_trades(trades, legs)
        self.assertEqual(ghost["trade_id"], "154")
        self.assertEqual(ghost["leg_count"], 1)
        self.assertIn("no trade name", " ".join(ghost["reasons"]))

    def test_healthy_and_legless_trades_are_not_ghosts(self):
        legs = [{**one_leg(), "trade_id": "153"}]
        self.assertEqual(one_reader.find_ghost_trades([
            trade_row(trade_id="153", margin=11535.36),      # real risk
            trade_row(trade_id="900", margin=0.0),           # zero risk, no legs
            trade_row(trade_id="901", margin=0.0, status="Closed"),
        ], legs), [])

    def test_unnamed_trade_with_real_risk_is_not_a_ghost(self):
        """A blank name alone is too weak -- a hand-built trade may be unnamed."""
        legs = [{**one_leg(), "trade_id": "153"}]
        self.assertEqual(one_reader.find_ghost_trades(
            [trade_row(trade_id="153", name="", margin=8953.5)], legs), [])

    def test_missing_risk_column_flags_nothing(self):
        legs = [{**one_leg(), "trade_id": "153"}]
        row = trade_row(trade_id="153", margin=0.0)
        row.update(margin=None, has_margin_column=False)
        self.assertEqual(one_reader.find_ghost_trades([row], legs), [])

    def test_ghost_trade_breaks_the_all_clear_even_when_legs_reconcile(self):
        """A ghost's legs are in the report, so they match -- the trade is still
        unmanageable in ONE and must not be reported as all-clear."""
        result = reconcile.reconcile_snapshots(
            {"captured_at": "now", "fills_today": [],
             "legs": [option(account="U1", qty=-1.0)]},
            {"source_file": "report.csv",
             "positions": [{**option(qty=-1.0), "avg_price": 10.0}],
             "ghost_trades": [{
                 "account": "GroupA", "trade_id": "154", "trade_name": "x",
                 "underlying": "SPX", "expiration": "20260807",
                 "open_date": "8/08/2026", "margin": 0.0, "leg_count": 1,
                 "reasons": ["ONE models no risk for it"],
                 "legs": [{"tradingClass": "SPXW", "expiry": "20260807",
                           "strike": 7400.0, "right": "P", "qty": -1.0,
                           "open_price": 10.0}],
             }]},
            config(),
        )
        self.assertEqual(result["accounts"]["U1"][0]["status"], "MATCH")
        [ghost] = result["ghost_trades"]
        self.assertEqual(ghost["ibkr_account"], "U1")

    def test_monthly_expiry_offset_does_not_mark_live_trade_closed(self):
        current = {
            **option(account="U1", qty=-1.0),
            "expiry": "20260820",
        }
        fill = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260820",
            "strike": 7400.0, "right": "P", "shares": 1.0,
            "side": "BOT", "price": 9.0, "time": "2026-07-28T10:00:00+00:00",
        }
        one_leg = {
            **option(account="GroupA", qty=-1.0),
            "expiry": "20260822", "expiry_listed": "20260821",
            "trade_id": "100", "trade_name": "Monthly",
        }
        activity = reconcile.classify_activity(
            [fill], [current],
            one_trades=[{
                "account": "GroupA", "trade_id": "100",
                "trade_name": "Monthly", "legs": [one_leg],
            }],
            account_map=config()["account_map"],
        )
        self.assertEqual(len(activity["by_trade"]), 1)
        self.assertNotEqual(
            activity["by_trade"][0]["status"], "TRADE_CLOSED")

    def test_adjustments_are_sorted_by_latest_execution_time(self):
        early = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260807",
            "strike": 7400.0, "right": "P", "shares": 1.0,
            "side": "BOT", "price": 9.0,
            "time": "2026-07-28T10:00:00+00:00",
        }
        late = {
            **early,
            "account": "U2",
            "strike": 7500.0,
            "time": "2026-07-28T11:00:00+00:00",
        }
        positions = [
            {**option(account="U1"), "strike": 7400.0},
            {**option(account="U2"), "strike": 7500.0},
        ]
        activity = reconcile.classify_activity([early, late], positions)
        self.assertEqual(
            [item["timestamp"] for item in activity["by_trade"]],
            [late["time"], early["time"]],
        )
        self.assertEqual(activity["opened"][0]["timestamp"], late["time"])

    def test_separate_executions_for_same_one_trade_are_not_merged(self):
        first = {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260814",
            "strike": 7200.0, "right": "P", "shares": 2.0,
            "side": "BOT", "price": 24.6, "permId": 100,
            "time": "2026-07-30T19:24:50+00:00",
        }
        second = {
            **first, "expiry": "20260828", "strike": 7575.0,
            "shares": 1.0, "side": "SLD", "price": 173.88,
            "permId": 200, "time": "2026-07-30T19:32:11+00:00",
        }
        one_trade = {
            "account": "GroupA", "trade_id": "122",
            "trade_name": "14 AUG.STRAT1.MAIN.SPX - 2 LOTS",
            "legs": [
                {**option(), "expiry": "20260814", "strike": 7200.0},
                {**option(), "expiry": "20260828", "strike": 7575.0},
            ],
        }
        positions = [
            {**option(account="U1", qty=2.0), "expiry": "20260814",
             "strike": 7200.0},
            {**option(account="U1", qty=-1.0), "expiry": "20260828",
             "strike": 7575.0},
        ]
        activity = reconcile.classify_activity(
            [first, second], positions, one_trades=[one_trade],
            account_map={"GroupA": "U1"},
        )
        self.assertEqual(len(activity["by_trade"]), 2)
        self.assertEqual(
            [item["timestamp"] for item in activity["by_trade"]],
            [second["time"], first["time"]],
        )
        self.assertEqual(
            [item["trade_id"] for item in activity["by_trade"]],
            ["122", "122"],
        )


class OptionStratReconciliationTests(unittest.TestCase):
    def test_price_difference_makes_combo_unclean(self):
        one_combo = {
            "name": "ONE", "code": "MAIN",
            "legs": [{
                "tradingClass": "SPXW", "expiry": "20260807",
                "right": "P", "strike": 7400.0, "qty": -2.0,
                "price": 10.0,
            }],
        }
        os_combo = {
            "name": "OS", "code": "MAIN",
            "legs": [{
                "tradingClass": "SPXW", "expiry": "20260807",
                "right": "P", "strike": 7400.0, "qty": -2.0,
                "price": 10.5,
            }],
        }
        diff = recon_one_os.diff_pair(one_combo, os_combo, 0.10)
        self.assertFalse(diff["clean"])
        self.assertEqual(diff["legs"][0]["status"], "PRICE_DIFF")


class DashboardAdjustmentTests(unittest.TestCase):
    def test_adjustment_view_has_time_sort_and_composable_filters(self):
        timestamp = "2026-07-28T11:00:00+00:00"
        opened = {
            "account": "U1", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260807",
            "strike": 7400.0, "right": "P", "label": "SPXW 2026-08-07 7400P",
            "qty": -1.0, "px": 10.0, "timestamp": timestamp,
            "one_trade_id": "117", "one_trade_name": "MAIN.SPX.TEST",
        }
        activity = {
            "rolled": [], "opened": [opened], "closed": [], "changed": [],
            "by_trade": [{
                "account": "U1", "trade_id": "117",
                "trade_name": "MAIN.SPX.TEST", "status": "ADJUSTED",
                "status_label": "ADJUSTED", "timestamp": timestamp,
                "underlying": "SPX", "rolled": [], "opened": [opened],
                "closed": [], "changed": [], "wizard_hint": "Update Trade #117",
            }],
        }
        with dashboard._lock:
            original = dict(dashboard._state)
            dashboard._state.update({
                "status": "ok", "result": {
                    "accounts": {}, "ignore_one_accounts": [],
                    "unmapped_one_groups": {}, "fills_today": [],
                },
                "error": None, "last_cycle": timestamp,
                "ibkr_connected": True, "one_file": "report.csv",
                "one_mtime": time.time(), "activity": activity,
                "account_codes": {"U1": "MAIN"}, "os_strategies": [],
                "flex": {}, "naming": [],
            })
        try:
            page = dashboard.render_html()
        finally:
            with dashboard._lock:
                dashboard._state.clear()
                dashboard._state.update(original)
        self.assertIn("<option value='time' selected>", page)
        for control_id in (
            "adj-filter-ticker", "adj-filter-strike", "adj-filter-trade",
            "adj-filter-account-type", "adj-filter-account",
            "adj-filter-status",
        ):
            self.assertIn(f"id='{control_id}'", page)
        self.assertIn('"account_type": "MAIN"', page)
        self.assertIn('"strikes": [7400.0]', page)
        self.assertIn('"timestamp_epoch":', page)
        self.assertIn("border:2px solid #1f6feb", page)
        self.assertIn("background:#1f6feb22", page)
        self.assertNotIn("background:#3a2d0a", page)


    def test_price_flag_row_carries_the_price_and_the_dollars(self):
        """A price flag the user cannot price is a flag they cannot act on."""
        finding = {
            "status": "PRICE_DRIFT", "label": "RUT 2026-09-17 2950P",
            "ibkr_qty": 4.0, "ibkr_px": 26.3958, "one_qty": 4.0,
            "one_px": 46.0467, "px_delta": -19.6509,
            "pnl_divergence": -7860.36, "underlying": "RUT",
        }
        with dashboard._lock:
            original = dict(dashboard._state)
            dashboard._state.update({
                "status": "ok",
                "result": {"accounts": {"U1000001": [finding]},
                           "ignore_one_accounts": [], "unmapped_one_groups": {},
                           "fills_today": []},
                "error": None, "last_cycle": "2026-08-29T06:35:00+00:00",
                "ibkr_connected": True, "one_file": "report.csv",
                "one_mtime": time.time(), "activity": {},
                "account_codes": {}, "os_strategies": [], "flex": {}, "naming": [],
            })
        try:
            page = dashboard.render_html()
        finally:
            with dashboard._lock:
                dashboard._state.clear()
                dashboard._state.update(original)
        self.assertIn("46.05", page)      # ONE's price, so the user knows what to change
        self.assertIn("-19.65", page)     # how far apart they are
        self.assertIn("-7,860", page)     # what it is worth
        self.assertIn("PRICE_DRIFT", page)

class FlexImportCompletenessTests(unittest.TestCase):
    @staticmethod
    def fill(*, exec_id="E1", qty=1.0):
        return {
            "account": "U1", "secType": "OPT", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260821",
            "strike": 7400.0, "right": "P", "shares": abs(qty),
            "side": "BOT" if qty > 0 else "SLD", "price": 10.0,
            "time": "2026-08-20T15:00:00+00:00", "execId": exec_id,
        }

    @staticmethod
    def snapshot(qty, fills):
        legs = [] if qty == 0 else [{
            **option(account="U1", qty=qty), "expiry": "20260821",
        }]
        return {
            "captured_at": "2026-08-20T16:00:00+00:00",
            "managed_accounts": ["U1"], "legs": legs,
            "fills_today": fills,
        }

    def test_journal_persists_deduplicates_and_detects_missing_fill(self):
        now = datetime(2026, 8, 20, 16, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "journal.json")
            _, checks = flex_export.update_execution_journal(
                self.snapshot(0, []), path, now)
            self.assertEqual(checks["U1"]["status"], flex_export.COMPLETE)

            fill = self.fill()
            merged, checks = flex_export.update_execution_journal(
                self.snapshot(1, [fill]), path, now)
            self.assertEqual(checks["U1"]["status"], flex_export.COMPLETE)
            self.assertEqual(len(merged["fills_today"]), 1)

            merged, _ = flex_export.update_execution_journal(
                self.snapshot(1, [fill]), path, now)
            self.assertEqual(len(merged["fills_today"]), 1)

            _, checks = flex_export.update_execution_journal(
                self.snapshot(2, [fill]), path, now)
            self.assertEqual(
                checks["U1"]["status"], flex_export.POSSIBLY_INCOMPLETE)
            self.assertEqual(
                checks["U1"]["discrepancies"][0]["unexplained_qty"], 1)

    def test_incomplete_import_is_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [["20260821,010000", "BUY", "OPT", "symbol", "1",
                     "10", "", "SPX", "-1000"]]
            normal = os.path.join(tmp, "ONEImport_U1.csv")
            flex_export._write_csv(normal, rows)
            paths = flex_export.save_account_files(
                {},
                {"U1": {"status": flex_export.POSSIBLY_INCOMPLETE}},
                tmp,
            )
            # The superseded file must not remain importable. It is deleted
            # rather than parked as .stale: it is regenerable from the
            # execution journal, and a leftover copy beside the current export
            # is what gets imported into ONE by mistake.
            self.assertFalse(os.path.exists(normal))
            self.assertFalse(os.path.exists(normal + ".stale"))
            self.assertIn(flex_export.POSSIBLY_INCOMPLETE, paths["U1"])

    def test_non_option_fills_remain_visible_but_are_not_exported(self):
        now = datetime(2026, 8, 20, 16, tzinfo=timezone.utc)
        stock_fill = {
            "account": "U1", "secType": "STRAT11", "underlying": "SPY",
            "tradingClass": "SPY", "shares": 1, "side": "BOT",
            "price": 650, "time": "2026-08-20T15:00:00+00:00",
            "execId": "STOCK1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            merged, _ = flex_export.update_execution_journal(
                self.snapshot(0, [stock_fill]),
                os.path.join(tmp, "journal.json"), now)
            self.assertEqual(merged["fills_today"], [stock_fill])
            rows, skipped = flex_export.generate(merged)
            self.assertEqual(dict(rows), {})
            self.assertEqual(skipped, 1)



class RiskChartTests(unittest.TestCase):
    """The Risk page draws its own SVG, so a malformed string is a blank chart
    rather than an exception.  Parse every chart as XML to catch that."""

    def assert_svg(self, markup):
        import xml.etree.ElementTree as ET
        self.assertIn("<svg", markup)
        ET.fromstring(markup)          # raises ParseError if the browser would choke
        return markup

    def test_diverging_bars_renders_both_signs_around_a_zero_line(self):
        svg = self.assert_svg(dashboard._diverging_bars([
            ("RUT", 999.0, "RUT theta", None),
            ("NFLX", -2.0, "NFLX theta", None),
        ]))
        self.assertIn("RUT", svg)
        self.assertIn("#3fb950", svg)   # positive bar
        self.assertIn("#ff7b72", svg)   # negative bar

    def test_diverging_bars_skips_missing_values_and_survives_all_zero(self):
        svg = self.assert_svg(dashboard._diverging_bars([
            ("A", 0.0, "a", None), ("B", None, "b", None),
        ]))
        self.assertIn("A", svg)
        self.assertNotIn(">B<", svg)

    def test_diverging_bars_with_nothing_to_draw_says_so(self):
        out = dashboard._diverging_bars([("A", None, "a", None)])
        self.assertNotIn("<svg", out)
        self.assertIn("no data", out)

    def test_diverging_bars_escapes_a_hostile_label(self):
        svg = self.assert_svg(dashboard._diverging_bars(
            [("<script>x</script>", 1.0, "t & t", None)]))
        self.assertNotIn("<script>", svg)

    def test_line_chart_needs_two_points(self):
        self.assertNotIn("<svg", dashboard._line_chart([("2026-08-27", 1.0)]))
        self.assert_svg(dashboard._line_chart(
            [("2026-08-27", 447152.88), ("2026-08-28", 450910.5)]))

    def test_line_chart_survives_a_flat_series(self):
        # hi == lo would be a divide-by-zero when scaling to the plot height
        self.assert_svg(dashboard._line_chart(
            [("d1", 100.0), ("d2", 100.0), ("d3", 100.0)]))

    def test_line_chart_ignores_days_that_failed_to_log(self):
        self.assert_svg(dashboard._line_chart(
            [("d1", 100.0), ("d2", None), ("d3", 120.0)]))

    def test_meter_clamps_out_of_range_values(self):
        self.assertIn("width:100%", dashboard._meter(180, hi=60))
        self.assertIn("width:0%", dashboard._meter(-5))
        self.assertIn("&mdash;", dashboard._meter(None))

    def test_risk_page_renders_every_tab_from_a_real_snapshot(self):
        metrics = {
            "captured_at": "2026-08-29T06:35:00",
            "nav_total": 447559.72,
            "accounts": {"U1000002": {
                "NetLiquidation": 199833.82, "TotalCashValue": 133746.5,
                "GrossPositionValue": 2421248.97, "FullInitMarginReq": 183065.74,
                "FullMaintMarginReq": 166185.42, "ExcessLiquidity": 33648.4,
                "currency": "AUD", "headroom_pct": 16.83, "init_margin_util_pct": 91.6}},
            "iv": {}, "by_expiry": [{
                "underlying": "RUT", "expiry": "20260904", "expiry_label": "2026-09-04",
                "delta_dollars": 1000.0, "theta": 727.0, "vega": -752.0, "gamma": -1.0,
                "daily_pnl": 10.0, "daily_pnl_pct": 0.5, "unrealized_pnl": 5.0,
                "positions": 12}],
            "by_ticker": [{
                "underlying": "RUT", "delta": 7.41, "gamma": -3.42, "vega": -761.1,
                "theta": 998.8, "delta_dollars": 22311.2, "daily_pnl": 10126.6,
                "unrealized_pnl": 40009.4, "positions": 59, "greeks_missing": 0,
                "daily_pnl_pct": 0.92, "daily_pnl_pct_nav": 2.26, "iv": 0.1603,
                "iv_rank": 1.8, "iv_percentile": 0.8, "chg_pct": -3.8,
                "low_1y": 0.157, "high_1y": 0.331, "samples": 251}],
            "radar": {"within_days": 7, "assignment_risk": [], "expiring": []},
        }
        with dashboard._lock:
            dashboard._state["metrics"] = metrics
            dashboard._state["metrics_at"] = "2026-08-29 06:35:00"
            dashboard._state["result"] = {"pnl_divergence": {
                "net": -7397.25, "gross": 11350.59, "by_ticker": {"RUT": -6543.88},
                "worst": [{"account": "U1000001", "label": "RUT 2026-09-17 2950P",
                           "status": "MATCH_FIFO_AVG", "px_delta": -19.6509,
                           "pnl_divergence": -7860.36}]}}
        try:
            page = dashboard.render_risk_html()
        finally:
            with dashboard._lock:
                for k in ("metrics", "metrics_at", "result"):
                    dashboard._state.pop(k, None)

        for panel in ("risk-overview", "risk-greeks", "risk-expiry",
                      "risk-margin", "risk-basis"):
            self.assertIn(f"data-tab='{panel}'", page)
        self.assertEqual(page.count("<div"), page.count("</div>"))
        self.assertIn("Theta / day", page)            # KPI strip
        self.assertIn("RUT 2026-09-17 2950P", page)   # divergence carried through
        self.assertIn("<svg", page)

    def test_risk_page_before_the_first_collection_is_not_broken_html(self):
        with dashboard._lock:
            dashboard._state.pop("metrics", None)
        page = dashboard.render_risk_html()
        self.assertIn("Waiting for the first Greeks collection", page)
        self.assertEqual(page.count("<div"), page.count("</div>"))


class PortableEmailTests(unittest.TestCase):
    def test_report_has_no_localhost_dependency(self):
        ibkr = {"captured_at": "now", "legs": [
            {**option(account="U1")}], "fills_today": []}
        one = {"source_file": "missing-report.csv", "source_mtime": time.time(),
               "positions": [
            {**option(account="GroupA")}]}
        subject, body = email_report.generate_report_html(ibkr, one, config())
        self.assertIn("MATCH", subject)
        self.assertIn("self-contained", body)
        self.assertIn("OptionStrat reconciliation", body)
        self.assertNotIn("127.0.0.1", body)
        self.assertNotIn("localhost", body)
        self.assertNotIn("href=\"/", body)
        self.assertIn("id=\"report-controls\"", body)
        self.assertIn("id=\"report-account\"", body)
        self.assertIn("id=\"report-ticker\"", body)
        self.assertIn("id=\"report-strike\"", body)
        self.assertIn("id=\"report-trade\"", body)
        self.assertIn("id=\"report-status\"", body)
        self.assertIn("report-sortable", body)
        self.assertIn("function applyFilters()", body)

    def test_portable_report_adjustments_have_filter_facets(self):
        timestamp = "2026-07-30T19:24:50+00:00"
        opened = {
            "account": "U1", "underlying": "SPX",
            "tradingClass": "SPXW", "expiry": "20260814",
            "strike": 7200.0, "right": "P",
            "label": "SPXW 2026-08-14 7200P", "qty": 2.0,
            "px": 24.6, "timestamp": timestamp,
            "one_trade_id": "122", "one_trade_name": "MAIN.SPX.TEST",
        }
        activity = {
            "rolled": [], "opened": [opened], "closed": [], "changed": [],
            "by_trade": [{
                "account": "U1", "trade_id": "122",
                "trade_name": "MAIN.SPX.TEST", "status": "ADJUSTED",
                "status_label": "ADJUSTED", "timestamp": timestamp,
                "underlying": "SPX", "rolled": [], "opened": [opened],
                "closed": [], "changed": [], "wizard_hint": "Update Trade #122",
            }],
        }
        result = {
            "accounts": {"U1": []}, "unmapped_one_groups": {},
            "ignore_one_accounts": [], "activity": activity,
            "one_source_mtime": time.time(), "ibkr_source": timestamp,
        }
        cfg = config()
        cfg["account_codes"] = {"U1": "MAIN"}
        _, body = email_report.generate_report_html(
            {"captured_at": timestamp, "legs": [], "fills_today": []},
            {"source_file": "missing-report.csv", "source_mtime": time.time(),
             "positions": []},
            cfg,
            reconciliation_result=result,
        )
        self.assertIn("class='report-filterable info adjustment-card'", body)
        self.assertIn("data-account='U1'", body)
        self.assertIn("data-account-type='MAIN'", body)
        self.assertIn("data-ticker='SPX'", body)
        self.assertIn("data-strike='7200'", body)
        self.assertIn("data-trade='122'", body)
        self.assertIn("data-status='ADJUSTED'", body)
        message, _ = email_report.build_email_message(
            "subject", body, cfg, attachment_html=body)
        attachment_parts = [
            part for part in message.walk()
            if part.get_content_disposition() == "attachment"
        ]
        self.assertEqual(len(attachment_parts), 1)
        attached_html = attachment_parts[0].get_payload(
            decode=True).decode("utf-8")
        self.assertIn("function applyFilters()", attached_html)
        self.assertIn("id=\"report-adjustment-sort\"", attached_html)

    def test_message_contains_portable_html_attachment(self):
        message, target = email_report.build_email_message(
            "subject", "<html>report</html>", config(),
            attachment_html="<html>portable</html>",
        )
        self.assertEqual(target, "recipient@example.com")
        filenames = [part.get_filename() for part in message.walk()
                     if part.get_filename()]
        self.assertEqual(len(filenames), 1)
        self.assertTrue(filenames[0].startswith(
            "TWS_Matcher_Portable_Report_"))


if __name__ == "__main__":
    unittest.main()
