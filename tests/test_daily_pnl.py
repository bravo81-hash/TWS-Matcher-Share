"""Daily attribution regressions: broker daily values, not lifetime basis."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

import daily_pnl as daily
import market_metrics as metrics
import dashboard


def contract(cid=1, qty=0, account='U1', ticker='SPX', currency='USD', sec_type='OPT'):
    return dict(account=account, conId=cid, qty=qty, underlying=ticker,
                currency=currency, secType=sec_type, tradingClass=ticker,
                expiry='20261015', right='P', strike=7500, multiplier=100)


def fill(cid=1, account='U1', when='2026-10-02T19:30:00+00:00'):
    return {**contract(cid, account=account), 'time': when, 'execId': f'{account}-{cid}',
            'side': 'SLD', 'shares': 1, 'price': 53}


class UniverseTests(unittest.TestCase):
    def build(self, legs=(), fills=(), prior=None, day='2026-10-02'):
        return daily.build_universe({'legs': list(legs), 'fills_today': list(fills)},
                                    {'U1', 'U2'}, prior, day)

    def test_closed_fills_remain_and_deduplicate_by_account_contract(self):
        result = self.build([contract(qty=-1)], [fill(), fill(), fill(2), fill(account='U2')])
        self.assertEqual(len(result['contracts']), 3)
        self.assertEqual(result['contracts']['U1|1']['qty'], -1)
        self.assertEqual(result['contracts']['U1|2']['qty'], 0)
        self.assertEqual(result['contracts']['U2|1']['qty'], 0)

    def test_restarts_keep_closed_contracts_and_day_rollover_drops_them(self):
        first = self.build([contract(qty=2)], [fill(2)])
        restarted = self.build(prior=first)
        self.assertEqual(set(restarted['contracts']), {'U1|1', 'U1|2'})
        next_day = self.build(prior=first, day='2026-10-03')
        self.assertEqual(set(next_day['contracts']), {'U1|1'})  # vanished overnight holding
        stale = self.build(prior=first, day='2026-10-20')
        self.assertEqual(stale['contracts'], {})

    def test_new_york_day_not_local_day_filters_fills(self):
        result = self.build(fills=[fill(1, when='2026-10-03T01:30:00+00:00'),
                                  fill(2, when='2026-10-03T05:30:00+00:00')])
        self.assertEqual(set(result['contracts']), {'U1|1'})

    def test_advisor_parent_and_combo_fills_do_not_double_count_allocations(self):
        result = self.build(fills=[fill(), fill(account='F_PARENT'),
                                  {**fill(2), 'secType': 'BAG'}])
        self.assertEqual(set(result['contracts']), {'U1|1'})

    def test_legacy_fill_matches_richer_execution_before_flagging_missing_id(self):
        rich = fill()
        old = dict(rich)
        old.pop('conId')
        self.assertEqual(self.build(fills=[old, rich])['issues'], {})
        self.assertIn('U1', self.build(fills=[old])['issues'])

    def test_missing_timestamp_is_an_issue_not_silently_dropped(self):
        self.assertIn('U1', self.build(fills=[fill(when='unknown')])['issues'])


class AttributionTests(unittest.TestCase):
    def report(self, contracts, pnl, balances=None, account_pnl=None, fx=None, **kwargs):
        return daily.aggregate_daily(contracts, pnl, balances or {
            'U1': {'currency': 'AUD', 'NetLiquidation': 100000},
            'U2': {'currency': 'AUD', 'NetLiquidation': 200000}},
            account_pnl or {'U1': {'daily_pnl': 150}},
            fx or {('USD', 'AUD'): 1.5}, **kwargs)

    def test_closed_daily_uses_broker_daily_not_lifetime_realized_or_extra_commission(self):
        row = self.report([contract()], {('U1', 1): {
            'daily_pnl': -3183.22, 'realized_pnl': -9643.05, 'position': 0}})['U1']['by_ticker'][0]
        self.assertEqual(row['daily_pnl'], -3183.22)
        self.assertEqual(row['closed_contracts_pnl'], -3183.22)

    def test_partial_close_contract_is_counted_once_with_broker_total(self):
        result = self.report([contract(qty=-1), contract(qty=-1)], {
            ('U1', 1): {'daily_pnl': 300, 'position': -1}})['U1']
        self.assertEqual(result['native_totals'], {'USD': 300})
        self.assertEqual(result['by_ticker'][0]['open_contracts'], 1)

    def test_same_ticker_accounts_do_not_offset_each_other(self):
        result = self.report([contract(), contract(account='U2')], {
            ('U1', 1): {'daily_pnl': 100, 'position': 0},
            ('U2', 1): {'daily_pnl': -400, 'position': 0}})
        self.assertEqual(result['U1']['native_totals']['USD'], 100)
        self.assertEqual(result['U2']['native_totals']['USD'], -400)

    def test_same_ticker_currencies_remain_separate_and_fx_translates_once(self):
        result = self.report([contract(), contract(2, currency='AUD')], {
            ('U1', 1): {'daily_pnl': 100, 'position': 0},
            ('U1', 2): {'daily_pnl': 10, 'position': 0}},
            account_pnl={'U1': {'daily_pnl': 163}})['U1']
        self.assertEqual(result['native_totals'], {'USD': 100, 'AUD': 10})
        self.assertEqual(result['attributed_base'], 160)
        self.assertEqual(result['residual'], 3)
        self.assertEqual(result['broker_daily_pnl'], 163)

    def test_stock_daily_pnl_is_included(self):
        result = self.report([contract(2, 10, ticker='VAS', currency='AUD', sec_type='STRAT11')], {
            ('U1', 2): {'daily_pnl': 80, 'position': 10}})['U1']
        self.assertEqual(result['native_totals']['AUD'], 80)

    def test_missing_closed_contract_invalidates_ticker_and_total(self):
        result = self.report([contract(), contract(2, qty=1)], {
            ('U1', 2): {'daily_pnl': 1000, 'position': 1}})['U1']
        self.assertIsNone(result['by_ticker'][0]['daily_pnl'])
        self.assertEqual(result['by_ticker'][0]['open_contracts_pnl'], 1000)
        self.assertIsNone(result['by_ticker'][0]['closed_contracts_pnl'])
        self.assertIsNone(result['native_totals']['USD'])
        self.assertIsNone(result['attributed_base'])
        self.assertEqual(result['broker_daily_pnl'], 150)
        self.assertFalse(result['complete'])

    def test_position_changes_and_unset_values_are_unavailable(self):
        for response in ({'daily_pnl': 100, 'position': 1},
                         {'daily_pnl': metrics.DBL_MAX, 'position': 0},
                         {'daily_pnl': float('nan'), 'position': 0}, {}):
            result = self.report([contract()], {('U1', 1): response})['U1']
            self.assertFalse(result['complete'])
            self.assertIsNone(result['attributed_base'])

    def test_missing_fx_preserves_native_pnl_but_never_invents_a_conversion(self):
        result = self.report([contract(currency='EUR')], {
            ('U1', 1): {'daily_pnl': 100, 'position': 0}})['U1']
        self.assertEqual(result['native_totals']['EUR'], 100)
        self.assertIsNone(result['attributed_base'])
        self.assertIsNone(result['residual'])

    def test_execution_gap_invalidates_account_attribution_total(self):
        result = self.report([contract()], {('U1', 1): {'daily_pnl': 100, 'position': 0}},
            execution_completeness={'U1': {'status': 'POSSIBLY_INCOMPLETE'}})['U1']
        self.assertFalse(result['complete'])
        self.assertIsNone(result['native_totals']['USD'])

    def test_missing_fills_do_not_suppress_daily_pnl_for_a_broker_covered_contract(self):
        result = self.report([contract(qty=2)], {('U1', 1): {'daily_pnl': 100, 'position': 2}},
            execution_completeness={'U1': {'status': 'POSSIBLY_INCOMPLETE',
                'discrepancies': [{'instrument': 'SPX 20261015 7500P', 'position_qty': 2}]}})['U1']
        self.assertTrue(result['complete'])
        self.assertEqual(result['native_totals']['USD'], 100)
        self.assertTrue(result['notes'])

    def test_unobserved_disappearing_contract_is_not_declared_covered(self):
        result = self.report([contract(qty=2)], {('U1', 1): {'daily_pnl': 100, 'position': 2}},
            execution_completeness={'U1': {'status': 'POSSIBLY_INCOMPLETE',
                'discrepancies': [{'instrument': 'SPX 20261015 7550P', 'position_qty': 0}]}})['U1']
        self.assertFalse(result['complete'])

    def test_residual_is_disclosed_and_never_spread_across_tickers(self):
        result = self.report([contract()], {('U1', 1): {'daily_pnl': 100, 'position': 0}},
                             account_pnl={'U1': {'daily_pnl': 400}})['U1']
        self.assertEqual(result['by_ticker'][0]['daily_pnl'], 100)
        self.assertEqual(result['residual'], 250)
        self.assertEqual(result['reconciliation_status'], 'difference')

    def test_unknown_account_currency_is_not_a_valid_conversion(self):
        result = self.report([contract()], {('U1', 1): {'daily_pnl': 100, 'position': 0}},
                             balances={'U1': {}})['U1']
        self.assertFalse(result['complete'])
        self.assertIsNone(result['attributed_base'])


class CollectionAndUITests(unittest.TestCase):
    def test_zero_positions_are_requested_once_and_subscriptions_cancelled(self):
        requests, cancelled = [], []
        def request(a, m, cid):
            requests.append((a, cid))
            return NS(dailyPnL=120, unrealizedPnL=0, realizedPnL=-900, value=0, position=0)
        position = NS(account='U1', position=0, contract=NS(conId=1))
        ib = NS(reqPnLSingle=request, sleep=lambda _: None,
                cancelPnLSingle=lambda a, m, cid: cancelled.append((a, cid)))
        result = metrics.collect_position_pnl(ib, [position, position])
        self.assertEqual(requests, [('U1', 1)])
        self.assertEqual(cancelled, requests)
        self.assertEqual(result[('U1', 1)]['daily_pnl'], 120)

    def test_account_subscription_cancelled_when_contract_request_fails(self):
        cancelled = []
        ib = NS(reqPnL=lambda a, m: NS(), cancelPnL=lambda a, m: cancelled.append(a))
        with patch.object(metrics, 'collect_position_pnl', side_effect=RuntimeError('lost')):
            with self.assertRaises(RuntimeError):
                metrics.collect_daily_sources(ib, [], {'U1'})
        self.assertEqual(cancelled, ['U1'])

    def test_fx_direction_for_usd_into_aud(self):
        subscribed, cancelled = [], []
        ticker = NS(bid=float('nan'), ask=float('nan'))
        def quote(_):
            ticker.bid = ticker.ask = 0.5
        ib = NS(reqMktData=lambda c, *args: (subscribed.append(c) or ticker),
                qualifyContracts=lambda c: [c],
                cancelMktData=lambda c: cancelled.append(c), sleep=quote)
        rates = metrics.collect_fx_rates(ib, {'USD'}, {'AUD'})
        self.assertEqual(subscribed[0].symbol, 'AUD')
        self.assertEqual(subscribed[0].currency, 'USD')
        self.assertEqual(rates[('USD', 'AUD')], 2)
        self.assertEqual(cancelled, subscribed)

    def test_account_selection_filters_greeks_radar_balances_cost_basis_and_curve(self):
        per_account = {}
        for a, ticker in [('U1', 'SPX'), ('U2', 'RUT')]:
            per_account[a] = dict(accounts={a: {'currency': 'AUD', 'NetLiquidation': 100}},
                                 nav_total=100, nav_currency='AUD', radar={}, by_expiry=[],
                                 by_ticker=[dict(underlying=ticker, currency='USD', positions=1)],
                                 daily={'complete': True, 'by_ticker': []})
        findings = {a: [dict(underlying=ticker, label=ticker + ' basis',
                            pnl_divergence=20, status='MATCH_FIFO_AVG')]
                    for a, ticker in [('U1', 'SPX'), ('U2', 'RUT')]}
        state = dict(metrics={'per_account': per_account}, result={'accounts': findings},
                     ibkr_connected=True, metrics_at=datetime.now(timezone.utc).isoformat())
        with patch.object(dashboard, '_state', state):
            html = dashboard.render_risk_html('U2')
        self.assertIn('Account: U2', html)
        self.assertIn('RUT basis', html)
        self.assertNotIn('SPX basis', html)
        self.assertNotIn('<b>SPX</b>', html)
        self.assertIn('/risk/check?account=U2', html)
        self.assertIn('How much did I make today by ticker?', html)

    def test_previously_cached_model_greek_is_not_counted_as_fresh(self):
        ticker = NS(modelGreeks=NS(theta=-1))
        c = NS(secType='OPT', conId=1, exchange='SMART')
        ib = NS(reqMktData=lambda c, **kw: ticker, sleep=lambda _: None,
                cancelMktData=lambda c: None)
        self.assertEqual(metrics.collect_greeks(ib, [NS(contract=c, position=1)]), {})

    def test_new_fill_refreshes_before_the_five_minute_timer(self):
        old_signature = ([], [], datetime.now(dashboard.ET).date().isoformat())
        state = {'metrics': {'source_signature': old_signature},
                 'ibkr_snap': {'legs': [], 'fills_today': [{'account': 'U1', 'execId': 'new'}]}}
        with patch.object(dashboard, '_state', state), \
             patch.object(dashboard, '_metrics_last', [dashboard.time.time()]), \
             patch.object(metrics, 'collect_all', return_value={}) as collect, \
             patch.object(dashboard, '_log_nav'):
            dashboard.maybe_collect_metrics(NS(), {}, force=False)
        collect.assert_called_once()

    def test_greeks_and_expiry_aggregate_separately_for_each_account(self):
        def held(a, qty):
            return NS(account=a, position=qty, contract=NS(secType='OPT', conId=1,
                symbol='SPX', tradingClass='SPX', multiplier=100, currency='USD',
                lastTradeDateOrContractMonth='20261015', strike=7500, right='P'))
        positions = [held('U1', -1), held('U2', 1)]
        quote = {1: dict(delta=.3, gamma=.002, vega=8, theta=-1,
                         und_price=7600, opt_price=50, market_data_type=1)}
        balances = {a: dict(currency='USD', NetLiquidation=100000) for a in ('U1', 'U2')}
        pnl = {(a, 1): dict(position=qty, daily_pnl=qty*100) for a, qty in [('U1', -1), ('U2', 1)]}
        with patch.object(metrics, 'collect_greeks', return_value=quote), \
             patch.object(metrics, 'collect_iv_stats', return_value={}), \
             patch.object(metrics, 'collect_account_values', return_value=balances), \
             patch.object(metrics.daily_pnl, 'load_universe', return_value={
                 'contracts': {daily.key(c): c for c in [contract(qty=-1), contract(qty=1, account='U2')]},
                 'trade_date': '2026-10-02', 'issues': {}}), \
             patch.object(metrics, 'collect_fx_rates', return_value={}), \
             patch.object(metrics, 'collect_daily_sources', return_value=(pnl, {})):
            result = metrics.collect_all(NS(positions=lambda: positions), {'U1', 'U2'}, snapshot={})
        self.assertEqual(result['per_account']['U1']['by_ticker'][0]['theta'], 100)
        self.assertEqual(result['per_account']['U2']['by_ticker'][0]['theta'], -100)
        self.assertEqual(result['per_account']['U1']['by_expiry'][0]['delta_dollars'], -228000)
        self.assertEqual(result['per_account']['U2']['by_expiry'][0]['delta_dollars'], 228000)


if __name__ == '__main__':
    unittest.main()
