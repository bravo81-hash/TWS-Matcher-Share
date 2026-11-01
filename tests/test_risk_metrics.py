"""Numerical and missing-data regressions for the Risk & Greeks audit."""
import math
import tempfile
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

import dashboard
import market_metrics as metrics


def position(con_id=1, qty=-2, symbol="SPX", currency="USD", expiry="20261002"):
    return NS(account="U1", position=qty, contract=NS(
        secType="OPT", conId=con_id, symbol=symbol, tradingClass=symbol,
        currency=currency, lastTradeDateOrContractMonth=expiry, multiplier="100",
        right="P", strike=7500, exchange="SMART"))


def greek(**changes):
    return dict(delta=-0.3, gamma=0.002, theta=-1.2, vega=8,
                und_price=7600, opt_price=50, market_data_type=1, **changes)


def pnl(**changes):
    return dict(daily_pnl=300, unrealized_pnl=-500, market_value=-10000, **changes)


class RiskMetricsTests(unittest.TestCase):
    def rollup(self, greeks=None, daily=None, nav_currency="AUD"):
        return metrics.aggregate_metrics(
            [position()], {("U1", 1): pnl() if daily is None else daily},
            {1: greek() if greeks is None else greeks}, {}, nav=100000,
            nav_currency=nav_currency)["by_ticker"][0]

    def test_signed_quantity_and_multiplier_for_each_greek(self):
        row = self.rollup()
        self.assertEqual(row["delta"], 60)
        self.assertAlmostEqual(row["gamma"], -0.4)
        self.assertEqual(row["theta"], 240)
        self.assertEqual(row["vega"], -1600)
        self.assertEqual(row["delta_dollars"], 456000)
        self.assertEqual(row["daily_pnl"], 300)  # already position P&L

    def test_nav_percentage_requires_matching_currency(self):
        self.assertIsNone(self.rollup()["daily_pnl_pct_nav"])
        self.assertEqual(self.rollup(nav_currency="USD")["daily_pnl_pct_nav"], 0.3)

    def test_daily_return_is_not_invented_from_current_market_value(self):
        row = self.rollup(daily={**pnl(), "market_value": 1000, "daily_pnl": 100})
        self.assertIsNone(row["daily_pnl_pct"])
        self.assertIsNone(row["gross_prior_value"])

    def test_missing_theta_does_not_invalidate_valid_delta_or_become_zero(self):
        row = self.rollup(greeks={**greek(), "theta": None})
        self.assertIsNone(row["theta"])
        self.assertEqual(row["delta"], 60)
        self.assertEqual(row["greeks_missing"], 1)
        self.assertEqual(row["missing_fields"]["theta"], 1)

    def test_missing_delta_does_not_discard_valid_vega(self):
        row = self.rollup(greeks={**greek(), "delta": None})
        self.assertIsNone(row["delta_dollars"])
        self.assertEqual(row["vega"], -1600)

    def test_missing_pnl_and_invalid_numbers_are_unavailable(self):
        for value in (None, float("nan"), float("inf"), metrics.DBL_MAX):
            with self.subTest(value=value):
                row = self.rollup(daily={**pnl(), "daily_pnl": value})
                self.assertIsNone(row["daily_pnl"])
                self.assertIsNone(row["daily_pnl_pct_nav"])

    def test_leg_changes_during_collection_invalidate_pnl(self):
        self.assertIsNone(self.rollup(daily={**pnl(), "position": -1})["daily_pnl"])

    def test_missing_hedge_does_not_produce_a_confident_total(self):
        result = metrics.aggregate_metrics(
            [position(), position(2, qty=2)], {}, {1: greek()}, {})
        self.assertIsNone(result["by_ticker"][0]["delta"])
        self.assertIsNone(dashboard._complete_total(result["by_ticker"], "delta_dollars"))

    def test_mixed_currencies_never_sum_into_one_book_total(self):
        self.assertIsNone(dashboard._complete_total(
            [{"currency": "USD", "theta": 10}, {"currency": "AUD", "theta": 20}], "theta"))

    def test_expiries_are_ordered_by_date_across_tickers(self):
        result = metrics.aggregate_metrics(
            [position(symbol="AAA", expiry="20261101"),
             position(2, symbol="ZZZ", expiry="20261001")], {}, {}, {})
        self.assertEqual(result["by_expiry"][0]["underlying"], "ZZZ")

    def test_date_clock_is_new_york_and_unknown_is_not_zero_dte(self):
        # Already Oct 2 in the far east; still Oct 1 in New York.
        now = datetime(2026, 10, 1, 20, tzinfo=timezone.utc)
        self.assertEqual(metrics._days_to_expiry("20261002", now), 1)
        self.assertIsNone(metrics._days_to_expiry("invalid", now))
        self.assertEqual(metrics._days_to_expiry("20260930", now), -1)

    def test_unknown_moneyness_is_not_otm(self):
        radar = metrics.assess_expiry_risk([position(symbol="SPY")], {}, as_of=date(2026, 10, 1))
        self.assertIsNone(radar["expiring"][0]["itm"])
        self.assertEqual(len(radar["unknown"]), 1)
        self.assertEqual(radar["assignment_risk"], [])

    def test_invalid_and_past_expiry_are_not_reported_as_expiring_today(self):
        radar = metrics.assess_expiry_risk(
            [position(expiry="bad"), position(2, expiry="20260930")], {},
            as_of=date(2026, 10, 1))
        self.assertEqual(radar["expiring"], [])
        self.assertEqual(len(radar["unknown"]), 1)

    def test_cash_settled_short_is_exempt_but_stock_option_is_flagged(self):
        data = {1: {**greek(), "und_price": 7400, "opt_price": 100.1}}
        for symbol, count in (("SPX", 0), ("SPY", 1)):
            radar = metrics.assess_expiry_risk([position(symbol=symbol)], data,
                                               as_of=date(2026, 10, 1))
            self.assertEqual(len(radar["assignment_risk"]), count)

    def test_account_base_currency_selection_is_order_independent(self):
        def value(tag, amount, currency):
            return NS(account="U1", tag=tag, value=str(amount), currency=currency, modelCode="")
        values = [value("NetLiquidation", 100000, "AUD"),
                  value("TotalCashValue", 50000, "AUD"),
                  value("TotalCashValue", 30000, "USD"),
                  value("TotalCashValue", 51000, "BASE"),
                  value("ExcessLiquidity", 20000, "AUD")]
        for rows in (values, list(reversed(values))):
            result = metrics.collect_account_values(NS(accountValues=lambda: rows))["U1"]
            self.assertEqual(result["TotalCashValue"], 51000)
            self.assertEqual(result["currency"], "AUD")
            self.assertEqual(result["headroom_pct"], 20)

    def test_theta_diagnostic_matches_finite_difference_and_rejects_bad_inputs(self):
        def price(t, right):
            s, k, r, q, vol = 100, 105, 0.04, 0.015, 0.2
            d1 = (math.log(s/k) + (r-q+vol*vol/2)*t)/(vol*math.sqrt(t))
            d2 = d1-vol*math.sqrt(t)
            n = lambda x: (1+math.erf(x/math.sqrt(2)))/2
            return (s*math.exp(-q*t)*n(d1)-k*math.exp(-r*t)*n(d2) if right == "C"
                    else k*math.exp(-r*t)*n(-d2)-s*math.exp(-q*t)*n(-d1))
        for right in ("C", "P"):
            expected = -(price(.5+1e-5, right)-price(.5-1e-5, right))/(2e-5*365)
            actual = metrics.bs_theta_per_day(100, 105, .5, .2, right, div=.015)
            self.assertAlmostEqual(actual, expected, places=8)
        self.assertIsNone(metrics.bs_theta_per_day(-100, 105, .5, .2, "C"))
        self.assertIsNone(metrics.bs_theta_per_day(100, 105, .5, .2, "bad"))

    def test_broker_theta_is_retained_and_shared_contracts_are_requested_once(self):
        calls, cancelled = [], []
        model = NS(impliedVol=.2, undPrice=7600, delta=-.3, gamma=.002,
                   vega=8, theta=-.7, optPrice=50)
        ticker = NS(modelGreeks=None, marketDataType=1)
        ib = NS(reqMktData=lambda c, **kw: (calls.append(c.conId) or
                ticker), sleep=lambda _: setattr(ticker, 'modelGreeks', model),
                cancelMktData=lambda c: cancelled.append(c.conId))
        result = metrics.collect_greeks(ib, [position(), position()])
        self.assertEqual(calls, [1])
        self.assertEqual(cancelled, [1])
        self.assertEqual(result[1]["theta"], -.7)
        self.assertEqual(result[1]["theta_source"], "ib")

    def test_market_subscriptions_are_cancelled_on_failure(self):
        cancelled = []
        def request(c, **kw):
            if c.conId == 2:
                raise RuntimeError("connection lost")
            return NS()
        ib = NS(reqMktData=request, cancelMktData=lambda c: cancelled.append(c.conId))
        with self.assertRaises(RuntimeError):
            metrics.collect_greeks(ib, [position(), position(2)])
        self.assertEqual(cancelled, [1])

    def test_risk_render_warns_about_stale_partial_data_and_currency(self):
        row = self.rollup(greeks={**greek(), "theta": None})
        state = {"metrics": {"by_ticker": [row], "nav_total": 100000,
                              "nav_currency": "AUD", "accounts": {}},
                 "metrics_at": "2026-01-01T00:00:00+00:00", "ibkr_connected": False}
        with patch.object(dashboard, "_state", state):
            page = dashboard.render_risk_html()
        self.assertIn("stale or TWS is disconnected", page)
        self.assertIn("Incomplete risk data", page)
        self.assertIn("no FX conversion", page)
        self.assertIn("/risk/check", page)
        self.assertNotIn("0.30%", page)
        self.assertEqual(page.count("<div"), page.count("</div>"))

    def test_iv_rank_percentile_dates_and_invalid_bars(self):
        bars = [NS(date=date(2026, 9, i+1), close=v)
                for i, v in enumerate([.1, .2, -1, float("nan"), .4, .3])]
        ib = NS(reqContractDetails=lambda _: [NS(contract=NS())],
                reqHistoricalData=lambda *args: bars)
        with tempfile.TemporaryDirectory() as directory:
            result = metrics.collect_iv_stats(ib, ["SPX"], "2026-10-01",
                                               directory + "/iv.json")["SPX"]
            self.assertAlmostEqual(result["iv_rank"], 200/3)
            self.assertEqual(result["iv_percentile"], 50)
            self.assertAlmostEqual(result["chg_pct"], -25)
            self.assertEqual(result["samples"], 4)
            self.assertEqual(result["iv_as_of"], "20260906")

    def test_forced_risk_refresh_bypasses_metrics_timer(self):
        state = {}
        with patch.object(dashboard, "_state", state), \
             patch.object(dashboard, "_metrics_last", [dashboard.time.time()]), \
             patch.object(metrics, "collect_all", return_value={}) as collect, \
             patch.object(dashboard, "_log_nav"):
            dashboard.maybe_collect_metrics(NS(), {}, force=False)
            collect.assert_not_called()
            dashboard.maybe_collect_metrics(NS(), {}, force=True)
            collect.assert_called_once()
            self.assertIsNone(state["metrics_error"])

    def test_pnl_subscriptions_are_cancelled_on_failure(self):
        cancelled = []
        def request(account, model, con_id):
            if con_id == 2:
                raise RuntimeError("connection lost")
            return NS()
        ib = NS(reqPnLSingle=request,
                cancelPnLSingle=lambda a, m, c: cancelled.append(c))
        with self.assertRaises(RuntimeError):
            metrics.collect_position_pnl(ib, [position(), position(2)])
        self.assertEqual(cancelled, [1])

    def test_missing_configured_account_invalidates_nav(self):
        ib = NS(positions=lambda: [])
        with patch.object(metrics, "collect_position_pnl", return_value={}), \
             patch.object(metrics, "collect_greeks", return_value={}), \
             patch.object(metrics, "collect_iv_stats", return_value={}), \
             patch.object(metrics.daily_pnl, "load_universe", return_value={
                 'contracts': {}, 'issues': {}, 'trade_date': '2026-10-02'}), \
             patch.object(metrics, "collect_fx_rates", return_value={}), \
             patch.object(metrics, "collect_daily_sources", return_value=({}, {})), \
             patch.object(metrics, "collect_account_values", return_value={
                 "U1": {"currency": "AUD", "NetLiquidation": 100}}):
            result = metrics.collect_all(ib, {"U1", "U2"})
        self.assertIsNone(result["nav_total"])
        self.assertEqual(result["missing_accounts"], ["U2"])

    def test_gamma_dollars_is_delta_dollar_change_for_one_percent(self):
        row = self.rollup()
        # -2 contracts * 100 * 0.002 = -0.4 shares/pt; * 7600^2 / 100
        self.assertAlmostEqual(row["gamma_dollars"], -0.4 * 7600 * 7600 / 100)
        self.assertIsNone(self.rollup(greeks={**greek(), "gamma": None})["gamma_dollars"])

    def test_local_greeks_match_finite_differences_and_iv_round_trips(self):
        S, K, T, v = 100.0, 105.0, .5, .25
        for right in ("C", "P"):
            g = metrics.bs_greeks(S, K, T, v, right)
            px = lambda s=S, t=T, vol=v: metrics.bs_price(s, K, t, vol, right)
            h = .01
            self.assertAlmostEqual(g["delta"], (px(S + h) - px(S - h)) / (2 * h), 5)
            self.assertAlmostEqual(g["gamma"], (px(S + h) - 2 * px() + px(S - h)) / h**2, 4)
            self.assertAlmostEqual(g["vega"], (px(vol=v + .005) - px(vol=v - .005)), 5)
            self.assertAlmostEqual(metrics.implied_vol(px(), S, K, T, right), v, 6)
        self.assertIsNone(metrics.implied_vol(.01, 100, 50, .5, "C"))  # below intrinsic

    def test_unquoted_option_gets_labelled_prior_close_estimate(self):
        opt = position(symbol="AAPL", expiry="20271217").contract
        opt.strike, opt.right = 250, "P"
        tickers, cancelled = {}, []

        def request(c, *args, **kw):
            return tickers.setdefault(c.symbol if c is not opt else "opt",
                                      NS(modelGreeks=None, marketDataType=1))

        def sleep(_):
            tickers["opt"].close = 12.0
            if "AAPL" in tickers:
                tickers["AAPL"].close = 255.0

        ib = NS(reqMktData=request, sleep=sleep, qualifyContracts=lambda c: [c],
                cancelMktData=lambda c: cancelled.append(c.symbol))
        g = metrics.collect_greeks(ib, [NS(account="U1", position=-1, contract=opt)])[1]
        self.assertEqual(g["greeks_source"], "prior_close_estimate")
        self.assertEqual((g["und_price"], g["opt_price"]), (255.0, 12.0))
        self.assertLess(g["delta"], 0)
        self.assertEqual(cancelled, ["AAPL", "AAPL"])  # option and underlying
        row = metrics.aggregate_metrics(
            [NS(account="U1", position=-1, contract=opt)], {}, {1: g}, {})["by_ticker"][0]
        self.assertEqual((row["estimated_greeks"], row["non_live_greeks"]), (1, 0))
        self.assertIsNotNone(row["delta_dollars"])

    def test_failed_collection_does_not_retry_every_cycle(self):
        old = ([], [], datetime.now(dashboard.ET).date().isoformat())
        state = {'metrics': {'source_signature': old},
                 'ibkr_snap': {'legs': [], 'fills_today': [{'account': 'U1', 'execId': 'x'}]}}
        with patch.object(dashboard, '_state', state), \
             patch.object(dashboard, '_metrics_last', [0.0]), \
             patch.object(dashboard, '_metrics_attempted', [None]), \
             patch.object(metrics, 'collect_all', side_effect=RuntimeError("TWS")) as collect:
            dashboard.maybe_collect_metrics(NS(), {}, force=False)
            dashboard.maybe_collect_metrics(NS(), {}, force=False)
        collect.assert_called_once()
        self.assertIn("TWS", state["metrics_error"])


if __name__ == "__main__":
    unittest.main()
