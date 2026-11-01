"""Broker daily P&L attribution, including contracts flat after today's trades.

Daily P&L is taken once per (account, conId) from reqPnLSingle. It already
includes the broker's daily trade accounting; do not add lifetime realized P&L
or subtract commissions again. The persisted universe survives restarts and
retains disappearing positions (expiry/assignment) for the current US day.
"""
from collections import defaultdict
from datetime import datetime, date
from zoneinfo import ZoneInfo
import math
import os

import snapshots

ET = ZoneInfo('America/New_York')
UNIVERSE_FILE = os.path.join(os.path.dirname(__file__), 'daily_pnl_universe.json')


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and abs(value) < 1e307 else None
    except (ValueError, TypeError):
        return None


def key(row):
    return f"{row.get('account')}|{row.get('conId')}"


def build_universe(snapshot, accounts, previous=None, trade_date=None):
    """Pure union of held, traded today, and previously observed contracts."""
    day = trade_date or datetime.now(ET).date().isoformat()
    previous = previous or {}
    wanted, held, issues = {}, {}, defaultdict(list)
    known = (previous.get('contracts', {}) if previous.get('trade_date') == day
             else previous.get('held', {}))
    try:
        age = (date.fromisoformat(day) - date.fromisoformat(previous['trade_date'])).days
        if not 0 <= age <= 7:
            known = {}
    except (KeyError, ValueError, TypeError):
        known = {}
    for row in known.values():
        if row.get('account') in accounts:
            wanted[key(row)] = dict(row, qty=0.0)
    for row in snapshot.get('legs', []):
        if row.get('account') not in accounts or not number(row.get('qty')):
            continue
        if row.get('secType') == 'BAG':
            continue  # combo cash flows must not duplicate the component legs
        if not row.get('conId'):
            issues[row['account']].append('Held contract has no broker conId')
            continue
        wanted[key(row)] = dict(row)
        held[key(row)] = dict(row)
    # Process richer new entries before legacy journal entries without IDs.
    for row in sorted(snapshot.get('fills_today', []), key=lambda r: not bool(r.get('conId'))):
        account = row.get('account')
        if account not in accounts or row.get('secType') == 'BAG':
            continue
        try:
            when = datetime.fromisoformat(str(row.get('time')).replace('Z', '+00:00'))
            if when.tzinfo is None:
                raise ValueError('missing timezone')
            if when.astimezone(ET).date().isoformat() != day:
                continue
        except (TypeError, ValueError):
            issues[account].append('Execution date/time is unavailable')
            continue
        if not row.get('conId'):
            # Old journal rows can lack IDs; match against the richer live
            # execution/position rows before declaring attribution incomplete.
            identity = ('account', 'secType', 'underlying', 'tradingClass',
                        'expiry', 'strike', 'right')
            if any(all(r.get(f) == row.get(f) for f in identity)
                   for r in wanted.values()):
                continue
            issues[account].append('Execution contract has no broker conId')
            continue
        wanted.setdefault(key(row), dict(row, qty=0.0))
    return {'version': 1, 'trade_date': day, 'contracts': wanted, 'held': held,
            'issues': dict(issues)}


def load_universe(snapshot, accounts, path=UNIVERSE_FILE):
    import json
    try:
        with open(path, encoding='utf-8') as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        previous = {}
    state = build_universe(snapshot, accounts, previous)
    snapshots.write_json_atomic(path, state)
    return state


def aggregate_daily(contracts, pnl, account_values, account_pnl, fx_rates=None,
                    issues=None, execution_completeness=None):
    """Attribute in native currency; disclose any base-currency difference.

    A missing contract invalidates its ticker/total. No residual is assigned
    to tickers. FX quotes translate the native total indicatively; the account
    daily P&L remains the authoritative broker base-currency figure.
    """
    fx_rates, issues = fx_rates or {}, issues or {}
    execution_completeness = execution_completeness or {}
    groups = {}
    for contract in {key(c): c for c in contracts}.values():
        a = contract['account']
        symbol = contract.get('underlying') or contract.get('localSymbol') or '?'
        currency = contract.get('currency') or 'unknown'
        if contract.get('secType') == 'CASH':
            symbol = f'{symbol}.{currency}'
        gkey = (a, symbol, currency)
        row = groups.setdefault(gkey, {
            'account': a, 'underlying': symbol, 'currency': currency,
            'daily_pnl': 0.0, 'open_contracts_pnl': 0.0, 'closed_contracts_pnl': 0.0,
            'open_contracts': 0, 'closed_contracts': 0, 'missing_contracts': 0,
            'missing_open': 0, 'missing_closed': 0,
        })
        current_qty = number(contract.get('qty'))
        closed = current_qty == 0
        component = 'closed_contracts_pnl' if closed else 'open_contracts_pnl'
        count = 'closed_contracts' if closed else 'open_contracts'
        row[count] += 1
        raw = pnl.get((a, contract['conId'])) or {}
        value = number(raw.get('daily_pnl'))
        if number(raw.get('position')) != current_qty:
            value = None  # no callback, or the book changed during collection
        if value is None or currency == 'unknown':
            row['missing_contracts'] += 1
            row['missing_closed' if closed else 'missing_open'] += 1
        else:
            row['daily_pnl'] += value
            row[component] += value

    result = {}
    for a, balances in account_values.items():
        rows = sorted((r for (acct, _, _), r in groups.items() if acct == a),
                      key=lambda r: (r['underlying'], r['currency']))
        base = balances.get('currency')
        reasons = list(issues.get(a, []))
        notes = []
        if not base or base == 'BASE':
            reasons.append('Account base currency is unavailable')
        ec = execution_completeness.get(a) or {}
        if ec.get('status') == 'POSSIBLY_INCOMPLETE':
            discrepancies = ec.get('discrepancies') or []
            covered = {}
            for c in contracts:
                if c.get('account') != a or c.get('secType') != 'OPT':
                    continue
                strike = number(c.get('strike'))
                if strike is None:
                    continue
                label = f"{c.get('tradingClass') or c.get('underlying')} {c.get('expiry')} {strike:g}{c.get('right')}"
                raw = pnl.get((a, c.get('conId'))) or {}
                if (number(raw.get('daily_pnl')) is not None
                        and number(raw.get('position')) == number(c.get('qty'))):
                    covered[label] = number(c.get('qty'))
            if discrepancies and all(d.get('instrument') in covered and
                    covered[d['instrument']] == number(d.get('position_qty')) for d in discrepancies):
                notes.append('Execution journal has unexplained changes for '
                             f'{len(discrepancies)} contract(s); broker daily P&L '
                             'is included for every affected contract. ONE import remains incomplete.')
            else:
                reasons.append('Execution journal has position changes without broker P&L coverage')
        totals = defaultdict(float)
        missing_currencies = set()
        translated = 0.0
        fx_missing = False
        for row in rows:
            if row['missing_contracts']:
                row['daily_pnl'] = None
                missing_currencies.add(row['currency'])
            if row['missing_open']:
                row['open_contracts_pnl'] = None
            if row['missing_closed']:
                row['closed_contracts_pnl'] = None
            cur = row['currency']
            rate = 1.0 if cur == base else number(fx_rates.get((cur, base)))
            row['fx_rate'] = rate
            row['daily_pnl_base'] = (row['daily_pnl'] * rate
                                     if row['daily_pnl'] is not None and rate else None)
            row['base_currency'] = base
            if row['daily_pnl'] is not None:
                totals[cur] += row['daily_pnl']
            if row['daily_pnl_base'] is None:
                fx_missing = True
            else:
                translated += row['daily_pnl_base']
        for cur in missing_currencies:
            totals[cur] = None
        if reasons:
            totals = {cur: None for cur in set(totals) | {r['currency'] for r in rows}}
        complete = not reasons and not any(r['missing_contracts'] for r in rows)
        total_base = translated if complete and not fx_missing else None
        broker_daily = number((account_pnl.get(a) or {}).get('daily_pnl'))
        residual = broker_daily - total_base if broker_daily is not None and total_base is not None else None
        nav = number(balances.get('NetLiquidation'))
        result[a] = {
            'by_ticker': rows, 'native_totals': dict(totals),
            'attributed_base': total_base, 'broker_daily_pnl': broker_daily,
            'currency': base, 'residual': residual,
            'daily_pnl_pct_nav': broker_daily / nav * 100 if broker_daily is not None and nav and nav > 0 else None,
            'complete': complete, 'issues': reasons, 'notes': notes,
            'missing_contracts': sum(r['missing_contracts'] for r in rows),
            'fx_is_indicative': any(r['currency'] != base for r in rows),
            'reconciliation_status': ('unavailable' if residual is None else
                                      'difference' if abs(residual) > 5 else 'within_tolerance'),
        }
    return result
