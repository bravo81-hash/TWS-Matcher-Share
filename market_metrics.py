#!/usr/bin/env python3
"""
Account, P&L, Greeks and volatility metrics from IBKR.

Everything the reconciliation core does not need, but the trader does:
  - per-account NAV / cash / margin / liquidity headroom   (no market data)
  - per-position daily and unrealised P&L                  (reqPnLSingle)
  - per-position model Greeks                              (market data, tick 106)
  - per-underlying IV with IV Rank and IV Percentile       (1Y daily IV history)
  - the settlement print an expired leg was priced on      (daily bar, cached)

Rollups per ticker and per expiry live in aggregate_metrics().

Pacing notes, learned by probing a live TWS:
  - reqPnLSingle and reqMktData each consume a per-connection subscription
    slot, and a book of ~160 options overruns the default 100 market-data
    lines, so both are issued in chunks and cancelled before the next chunk.
  - Historical IV is one pacing-limited request per underlying, and the series
    moves once a day, so a day's answer is cached on disk.
  - IBKR reports "no value" as DBL_MAX rather than null; _num() scrubs it.
"""

from __future__ import annotations

import json
import math
import os
import time
from types import SimpleNamespace
from datetime import date
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import daily_pnl

HERE = os.path.dirname(os.path.abspath(__file__))
IV_CACHE = os.path.join(HERE, "iv_cache.json")
SETTLEMENT_CACHE = os.path.join(HERE, "settlement_cache.json")

DBL_MAX = 1.7976931348623157e308
MKT_DATA_CHUNK = 45      # stay well under the default 100-line allowance
PNL_CHUNK = 45
IV_LOOKBACK = "1 Y"
ET = ZoneInfo("America/New_York")

# Account fields worth showing, in display order.
ACCOUNT_TAGS = [
    "NetLiquidation", "TotalCashValue", "GrossPositionValue",
    "FullInitMarginReq", "FullMaintMarginReq", "ExcessLiquidity",
    "AvailableFunds", "BuyingPower", "UnrealizedPnL", "RealizedPnL",
]

# Cash indices quote on their own exchange; everything else is SMART equity.
INDEX_EXCHANGE = {"SPX": "CBOE", "RUT": "RUSSELL", "NDX": "NASDAQ",
                  "VIX": "CBOE", "DJX": "CBOE", "XSP": "CBOE"}


def _num(x):
    """IBKR uses DBL_MAX (and NaN) for 'not set'. Return a real float or None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v) or abs(v) >= DBL_MAX / 10:
        return None
    return v


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ------------------------------------------------------------------- theta
# Keep the broker's model Greeks together. A local European Black-Scholes
# estimate with fixed carry assumptions is a diagnostic, not a replacement for
# IB's theta (especially for American options and contracts near settlement).
RISK_FREE_RATE = 0.04
DIVIDEND_YIELD = 0.0


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_theta_per_day(spot, strike, years, sigma, right,
                     rate=RISK_FREE_RATE, div=DIVIDEND_YIELD):
    """Black-Scholes theta per calendar day, per share. Negative = decay."""
    values = [_num(x) for x in (spot, strike, years, sigma, rate, div)]
    if any(x is None for x in values) or right not in ("C", "P"):
        return None
    spot, strike, years, sigma, rate, div = values
    if min(spot, strike, years, sigma) <= 0:
        return None
    vt = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * sigma * sigma) * years) / vt
    d2 = d1 - vt
    dfq, dfr = math.exp(-div * years), math.exp(-rate * years)
    decay = -spot * dfq * _norm_pdf(d1) * sigma / (2 * math.sqrt(years))
    if right == "C":
        annual = decay - rate * strike * dfr * _norm_cdf(d2)             + div * spot * dfq * _norm_cdf(d1)
    else:
        annual = decay + rate * strike * dfr * _norm_cdf(-d2)             - div * spot * dfq * _norm_cdf(-d1)
    return annual / 365.0


def bs_price(spot, strike, years, sigma, right, rate=RISK_FREE_RATE, div=DIVIDEND_YIELD):
    """European Black-Scholes price per share (inputs already validated)."""
    vt = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * sigma * sigma) * years) / vt
    d2 = d1 - vt
    dfq, dfr = math.exp(-div * years), math.exp(-rate * years)
    if right == "C":
        return spot * dfq * _norm_cdf(d1) - strike * dfr * _norm_cdf(d2)
    return strike * dfr * _norm_cdf(-d2) - spot * dfq * _norm_cdf(-d1)


def implied_vol(price, spot, strike, years, right,
                rate=RISK_FREE_RATE, div=DIVIDEND_YIELD):
    """Volatility that reproduces ``price``; None when no volatility can."""
    values = [_num(x) for x in (price, spot, strike, years)]
    if any(x is None or x <= 0 for x in values) or right not in ("C", "P"):
        return None
    price, spot, strike, years = values
    lo, hi = 1e-4, 5.0
    if not (bs_price(spot, strike, years, lo, right, rate, div) <= price
            <= bs_price(spot, strike, years, hi, right, rate, div)):
        return None   # below intrinsic or absurdly rich: no meaningful IV
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, years, mid, right, rate, div) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def bs_greeks(spot, strike, years, sigma, right,
              rate=RISK_FREE_RATE, div=DIVIDEND_YIELD):
    """Per-share delta, gamma, vega (per vol point) and theta (per day)."""
    values = [_num(x) for x in (spot, strike, years, sigma)]
    if any(x is None or x <= 0 for x in values) or right not in ("C", "P"):
        return None
    spot, strike, years, sigma = values
    vt = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - div + 0.5 * sigma * sigma) * years) / vt
    dfq = math.exp(-div * years)
    return {
        "delta": dfq * (_norm_cdf(d1) if right == "C" else _norm_cdf(d1) - 1),
        "gamma": dfq * _norm_pdf(d1) / (spot * vt),
        "vega": spot * dfq * _norm_pdf(d1) * math.sqrt(years) / 100,
        "theta": bs_theta_per_day(spot, strike, years, sigma, right, rate, div),
    }


def _days_to_expiry(expiry, as_of=None):
    try:
        e = date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
    except (TypeError, ValueError, IndexError):
        return None
    today = as_of or datetime.now(ET).date()
    if isinstance(today, datetime):
        today = today.astimezone(ET).date()
    return (e - today).days


def _years_to_expiry(expiry, as_of=None):
    days = _days_to_expiry(expiry, as_of)
    return days / 365.0 if days is not None and days > 0 else None


# --------------------------------------------------------------- accounts
def collect_account_values(ib, accounts=None) -> dict:
    """Per-account balances and margin. Needs no market-data subscription.

    ``headroom_pct`` is the current excess-liquidity/NAV ratio. It is not a
    loss limit: margin requirements can change as prices and volatility move.
    """
    raw: dict[str, dict] = defaultdict(dict)
    values = list(ib.accountValues())
    base = {v.account: v.currency.upper() for v in values
            if v.tag == "NetLiquidation" and v.currency
            and v.currency.upper() != "BASE" and not getattr(v, "modelCode", "")}
    for v in values:
        if v.tag not in ACCOUNT_TAGS:
            continue
        if accounts and v.account not in accounts:
            continue
        if getattr(v, "modelCode", ""):
            continue  # do not replace an account total with a model subtotal
        val = _num(v.value)
        if val is None:
            continue
        cur = (v.currency or "").upper()
        # Never let a currency-specific balance overwrite a base-currency
        # account total. Select deterministically, independent of row order.
        account_base = base.get(v.account)
        if cur not in ("", "BASE", account_base):
            continue
        if v.tag in raw[v.account] and cur != "BASE":
            continue
        raw[v.account][v.tag] = val
        raw[v.account]["currency"] = account_base

    for d in raw.values():
        nav = d.get("NetLiquidation")
        excess = d.get("ExcessLiquidity")
        init = d.get("FullInitMarginReq")
        d["headroom_pct"] = (excess / nav * 100) if nav and nav > 0 and excess is not None else None
        d["init_margin_util_pct"] = (init / nav * 100) if nav and nav > 0 and init is not None else None
    return dict(raw)


# ------------------------------------------------------------- position P&L
def collect_position_pnl(ib, positions) -> dict:
    """{(account, conId): daily / unrealised / value} via reqPnLSingle."""
    wanted = sorted({(p.account, p.contract.conId) for p in positions
                     if p.contract.conId})
    out: dict[tuple, dict] = {}
    for chunk in _chunks(wanted, PNL_CHUNK):
        subs = []
        try:
            for a, c in chunk:
                subs.append((a, c, ib.reqPnLSingle(a, "", c)))
            ib.sleep(4)
            for a, c, s in subs:
                out[(a, c)] = {
                    "account": a, "conId": c,
                    "daily_pnl": _num(s.dailyPnL),
                    "unrealized_pnl": _num(s.unrealizedPnL),
                    "realized_pnl": _num(s.realizedPnL),
                    "market_value": _num(s.value),
                    "position": _num(s.position),
                }
        finally:
            for a, c, _ in subs:
                ib.cancelPnLSingle(a, "", c)
    return out


def collect_daily_sources(ib, positions, accounts):
    """Collect account and contract daily P&L in the same sampling interval."""
    subs = []
    try:
        for account in sorted(accounts):
            subs.append((account, ib.reqPnL(account, '')))
        pnl = collect_position_pnl(ib, positions)
        if not positions:
            ib.sleep(4)
        account_pnl = {a: {'daily_pnl': _num(s.dailyPnL)} for a, s in subs}
        return pnl, account_pnl
    finally:
        for account, _ in subs:
            ib.cancelPnL(account, '')


def collect_fx_rates(ib, currencies, base_currencies):
    """Indicative broker FX quotes; never assume a fixed conversion rate."""
    from ib_async import Forex
    rank = {cur: i for i, cur in enumerate(('EUR', 'GBP', 'AUD', 'NZD',
                                           'USD', 'CAD', 'CHF', 'JPY'))}
    pairs = {(a, b) for a in currencies for b in base_currencies
             if a and b and a != b and a != 'unknown'}
    out, subs = {}, []
    try:
        for native, base in sorted(pairs):
            first, second = sorted((native, base), key=lambda c: rank.get(c, 99))
            contract = Forex(first + second)
            try:
                if not ib.qualifyContracts(contract):
                    continue
                ticker = ib.reqMktData(contract, '', False, False)
                ticker.bid = ticker.ask = float('nan')
                subs.append((native, base, first, contract, ticker))
            except Exception:
                continue  # optional FX data must not suppress native P&L
        if subs:
            ib.sleep(3)
        for native, base, first, _, ticker in subs:
            bid, ask = _num(ticker.bid), _num(ticker.ask)
            # No fallback to a stale last price or a guessed daily rate.
            mid = (bid + ask) / 2 if bid and ask and 0 < bid <= ask else None
            if mid:
                out[(native, base)] = mid if native == first else 1 / mid
    finally:
        for _, _, _, contract, _ in subs:
            ib.cancelMktData(contract)
    return out


# ----------------------------------------------------------------- Greeks
def collect_greeks(ib, positions) -> dict:
    """{conId: greeks} from IB's option model (generic tick 106).

    Keyed by contract rather than by position, so an option held in several
    accounts costs one market-data line instead of one per account.
    """
    uniq = {}
    for p in positions:
        c = p.contract
        if c.secType == "OPT" and p.position and c.conId not in uniq:
            uniq[c.conId] = c

    out: dict[int, dict] = {}
    unquoted: dict[int, tuple] = {}
    for chunk in _chunks(list(uniq.values()), MKT_DATA_CHUNK):
        live = []
        try:
            for c in chunk:
                if not c.exchange:
                    c.exchange = "SMART"
                ticker = ib.reqMktData(c, genericTickList="", snapshot=False)
                # ib_async reuses Ticker objects after cancellation. A previous
                # model quote must not masquerade as a fresh broker response.
                ticker.modelGreeks = None
                ticker.close = float("nan")
                live.append((c, ticker))
            ib.sleep(8)
            for c, t in live:
                g = t.modelGreeks
                if not g:
                    close = _num(getattr(t, "close", None))
                    if close is not None and close > 0:
                        unquoted[c.conId] = (c, close)
                if g:
                    iv, spot = _num(g.impliedVol), _num(g.undPrice)
                    years = _years_to_expiry(c.lastTradeDateOrContractMonth)
                    local = bs_theta_per_day(spot, _num(c.strike), years, iv, c.right)
                    out[c.conId] = {
                        "iv": iv, "delta": _num(g.delta),
                        "gamma": _num(g.gamma), "vega": _num(g.vega),
                        "theta": _num(g.theta),
                        "theta_ib": _num(g.theta),
                        "theta_estimate": local,
                        "theta_source": "ib",
                        "market_data_type": getattr(t, "marketDataType", None),
                        "opt_price": _num(g.optPrice), "und_price": spot,
                    }
        finally:
            for c, _ in live:
                ib.cancelMktData(c)
    out.update(_prior_close_greeks(ib, unquoted.values()))
    return out


def _prior_close_greeks(ib, unquoted) -> dict:
    """Estimate Greeks for options IB could not model from a live quote.

    Single-stock options only quote 09:30-16:00 New York, so before the open
    and after the close IB returns no model Greeks for them -- and a trader
    outside US time zones may look at the book mostly in those hours. Rather than
    leave those legs blank (which also blanks every total they belong to and
    hides exactly the assignable legs from the assignment radar), solve an IV
    from the option's prior close against the underlying's prior close and
    price Greeks from that consistent pair. Black-Scholes with fixed carry: an
    estimate, labelled as one, never mixed up with IB's model.
    """
    from ib_async import Index, Stock

    unquoted = list(unquoted)
    if not unquoted:
        return {}
    unders, subs, out = {}, [], {}
    try:
        for sym in sorted({c.symbol for c, _ in unquoted if c.symbol}):
            try:
                contract = (Index(sym, INDEX_EXCHANGE[sym], "USD")
                            if sym in INDEX_EXCHANGE else Stock(sym, "SMART", "USD"))
                if not ib.qualifyContracts(contract):   # needs a conId first
                    continue
                ticker = ib.reqMktData(contract, "", False, False)
                ticker.close = float("nan")
                subs.append(contract)
                unders[sym] = ticker
            except Exception:
                continue          # no underlying, no estimate -- stays missing
        if subs:
            ib.sleep(3)
        for c, opt_close in unquoted:
            t = unders.get(c.symbol)
            spot = _num(getattr(t, "close", None)) if t else None
            strike = _num(c.strike)
            years = _years_to_expiry(c.lastTradeDateOrContractMonth)
            iv = implied_vol(opt_close, spot, strike, years, c.right)
            g = bs_greeks(spot, strike, years, iv, c.right) if iv else None
            if not g:
                continue
            out[c.conId] = {
                "iv": iv, **g, "theta_ib": None, "theta_estimate": g["theta"],
                "theta_source": "prior_close_estimate",
                "greeks_source": "prior_close_estimate",
                "market_data_type": None,
                "opt_price": opt_close, "und_price": spot,
            }
    finally:
        for contract in subs:
            ib.cancelMktData(contract)
    return out


# ------------------------------------------------------------- IV / IVR / IVP
def _load_cache(path: str) -> dict:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_cache(path: str, data: dict) -> None:
    try:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
    except OSError:
        pass                  # a cache that will not write is not an error


def collect_iv_stats(ib, underlyings, trading_day: str | None = None,
                     cache_path: str = IV_CACHE) -> dict:
    """{symbol: iv, iv_rank, iv_percentile, chg, chg_pct} from 1Y daily IV.

    IV Rank places today's IV in the year's high-low range; IV Percentile is the
    share of days that closed below it. They answer different questions -- a
    single spike distorts rank but barely moves percentile -- so both are kept.
    """
    from ib_async import Index, Stock

    day = trading_day or datetime.now(ET).strftime("%Y-%m-%d")
    cache = _load_cache(cache_path)
    out, dirty = {}, False

    for sym in sorted({s for s in underlyings if s}):
        hit = cache.get(sym)
        if hit and hit.get("day") == day and hit.get("iv_as_of"):
            out[sym] = {k: v for k, v in hit.items() if k != "day"}
            continue
        try:
            contract = (Index(sym, INDEX_EXCHANGE[sym], "USD")
                        if sym in INDEX_EXCHANGE else Stock(sym, "SMART", "USD"))
            details = ib.reqContractDetails(contract)
            if not details:
                out[sym] = {"error": "no contract details"}
                continue
            bars = ib.reqHistoricalData(details[0].contract, "", IV_LOOKBACK,
                                        "1 day", "OPTION_IMPLIED_VOLATILITY",
                                        True, 1)
            valid_bars = [b for b in bars if _num(b.close) is not None
                          and _num(b.close) > 0]
            vals = [_num(b.close) for b in valid_bars]
            if len(vals) < 2:
                out[sym] = {"error": "no IV history"}
                continue
            cur, lo, hi, prev = vals[-1], min(vals), max(vals), vals[-2]
            rec = {
                "iv": cur,
                "iv_rank": ((cur - lo) / (hi - lo) * 100) if hi > lo else None,
                "iv_percentile": sum(1 for v in vals if v < cur) / len(vals) * 100,
                "chg": cur - prev,
                "chg_pct": ((cur - prev) / prev * 100) if prev else None,
                "low_1y": lo, "high_1y": hi, "samples": len(vals),
                "iv_as_of": _bar_day(valid_bars[-1]),
                "iv_retrieved_at": datetime.now(timezone.utc).isoformat(),
            }
            out[sym] = rec
            cache[sym] = {**rec, "day": day}
            dirty = True
        except Exception as exc:            # one bad symbol must not kill the run
            out[sym] = {"error": f"{type(exc).__name__}: {exc}"}

    if dirty:
        _save_cache(cache_path, cache)
    return out


# ------------------------------------------------- expiry & assignment radar
# Cash-settled, European index options cannot be assigned early and never turn
# into stock, so they are exempt from the assignment check (they still appear in
# the expiry radar, where they settle for cash).
CASH_SETTLED_ROOTS = {"SPX", "SPXW", "RUT", "RUTW", "NDX", "NDXP", "XSP",
                      "VIX", "VIXW", "DJX", "MXEA", "MXEF"}

# An in-the-money short American option is at risk of early exercise once its
# remaining extrinsic value is small enough that the holder gives up little by
# exercising. Below this many dollars per share, treat it as live risk.
ASSIGNMENT_EXTRINSIC = 0.50


def assess_expiry_risk(positions, greeks, within_days=7, as_of=None) -> dict:
    """Positions near expiry, and short options exposed to early assignment.

    Two different clocks. The expiry radar is about work that has to happen --
    roll, close, or settle -- before a contract disappears. The assignment radar
    is about something that can happen tonight without warning: an early
    assignment turns a short option into stock, which is how an account ends up
    short shares it never meant to hold.
    """
    today = as_of or datetime.now(ET).date()
    expiring, assignment, unknown = [], [], []
    for p in positions:
        c = p.contract
        if c.secType != "OPT" or not p.position:
            continue
        dte = _days_to_expiry(c.lastTradeDateOrContractMonth, today)
        g = greeks.get(c.conId) or {}
        spot, strike = g.get("und_price"), _num(c.strike)
        intrinsic = None
        if spot and strike:
            intrinsic = (max(0.0, spot - strike) if c.right == "C"
                         else max(0.0, strike - spot))
        px = g.get("opt_price")
        extrinsic = (px - intrinsic) if (px is not None and intrinsic is not None) else None
        row = {
            "account": p.account, "underlying": c.symbol,
            "tradingClass": c.tradingClass, "expiry": c.lastTradeDateOrContractMonth,
            "expiry_label": _expiry_label(c.lastTradeDateOrContractMonth),
            "strike": strike, "right": c.right, "qty": float(p.position),
            "dte": dte, "spot": spot, "delta": g.get("delta"),
            "intrinsic": intrinsic, "extrinsic": extrinsic,
            "itm": intrinsic > 0 if intrinsic is not None else None,
            "cash_settled": (c.tradingClass or c.symbol) in CASH_SETTLED_ROOTS
                            or c.symbol in CASH_SETTLED_ROOTS,
        }
        if dte is not None and 0 <= dte <= within_days:
            expiring.append(row)
        if dte is None or (p.position < 0 and not row["cash_settled"]
                           and (intrinsic is None or extrinsic is None)):
            unknown.append(row)
        if (p.position < 0 and not row["cash_settled"] and row["itm"]
                and extrinsic is not None and extrinsic <= ASSIGNMENT_EXTRINSIC):
            assignment.append(row)

    expiring.sort(key=lambda r: (r["dte"], r["underlying"], r["strike"] or 0))
    assignment.sort(key=lambda r: (r["extrinsic"] if r["extrinsic"] is not None
                                   else 99, r["dte"] if r["dte"] is not None else 9999))
    return {"expiring": expiring, "assignment_risk": assignment,
            "unknown": unknown,
            "within_days": within_days}


# ------------------------------------------------- expiry settlement prints
# What an expired option actually settles against. AM-settled index options
# settle on a Special Opening Quotation built from the constituents' opening
# prints (SPX -> SET, RUT -> RLS, VIX -> VRO). That number is published on its
# own ticker, not on the chain, and it is NOT the index's own open -- but the
# open is the closest proxy IBKR will give us, so it is labelled approximate.
# Everything else -- weekly index options and ETF options --
# settles against the 4pm close, which the daily bar simply IS.
AM_SETTLED_ROOTS = {"SPX", "RUT", "NDX", "DJX", "MXEA", "MXEF", "VIX", "VIXW"}

# Today's "close" is only the last trade so far, so it is never written to the
# disk cache -- but the daemon cycles every 30s and IB paces historical data,
# so hold it in memory for a few minutes rather than re-asking every cycle.
PROVISIONAL_TTL = 300
_provisional: dict = {}


def settlement_basis(trading_class, underlying=None) -> str:
    """Which daily-bar field settled this option: 'open' (AM/SOQ) or 'close'.

    The trading class decides it on its own -- RUTW is a PM weekly even though
    its underlying RUT settles its monthlies AM -- so the underlying is only
    consulted when ONE gave us no class at all.
    """
    root = str(trading_class or underlying or "").upper()
    return "open" if root in AM_SETTLED_ROOTS else "close"


def is_cash_settled(trading_class, underlying=None) -> bool:
    """Cash-settled legs vanish into a cash debit; the rest deliver shares."""
    return any(r and str(r).upper() in CASH_SETTLED_ROOTS
               for r in (trading_class, underlying))


def _bar_day(bar) -> str | None:
    d = getattr(bar, "date", None)
    if hasattr(d, "strftime"):
        return d.strftime("%Y%m%d")
    s = str(d or "").strip()[:10].replace("-", "")
    return s if len(s) == 8 and s.isdigit() else None


def _duration_for(oldest: str) -> str:
    """Just enough history to cover the oldest date asked for."""
    try:
        days = (date.today() - date(int(oldest[:4]), int(oldest[4:6]),
                                    int(oldest[6:8]))).days
    except (TypeError, ValueError, IndexError):
        days = 10
    return f"{max(10, days + 6)} D" if days <= 350 else "2 Y"


def collect_settlement_closes(ib, wants, cache_path: str = SETTLEMENT_CACHE) -> dict:
    """Daily OHLC for each (underlying, expiry) an expired leg needs pricing on.

    Returns ``{(SYMBOL, 'YYYYMMDD'): {open, high, low, close, bar_date,
    provisional}}``. One historical request per symbol covers every date asked
    of it. A past session's print never changes, so it is cached on disk for
    good; a date that is still today is returned but not cached, because the
    session may still be open and its "close" is only the last trade so far.

    Index contracts need their own exchange (RUT -> RUSSELL; CBOE errors 200),
    and a dated ``endDateTime`` is rejected as malformed, so this walks back
    from now instead. A symbol that fails is simply absent from the result --
    every caller falls back to the worthless case.
    """
    from ib_async import Index, Stock

    wants = {(str(sym).upper(), str(exp)) for sym, exp in wants if sym and exp}
    if not wants:
        return {}

    cache = _load_cache(cache_path)
    today = date.today().strftime("%Y%m%d")
    out, dirty = {}, False

    pending = defaultdict(set)
    now = time.time()
    for sym, exp in wants:
        hit = cache.get(f"{sym}|{exp}")
        if hit:
            out[(sym, exp)] = hit
            continue
        fresh = _provisional.get((sym, exp))
        if fresh and now - fresh[0] < PROVISIONAL_TTL:
            out[(sym, exp)] = fresh[1]
            continue
        pending[sym].add(exp)

    for sym, expiries in pending.items():
        try:
            contract = (Index(sym, INDEX_EXCHANGE[sym], "USD")
                        if sym in INDEX_EXCHANGE else Stock(sym, "SMART", "USD"))
            details = ib.reqContractDetails(contract)
            if not details:
                continue
            bars = ib.reqHistoricalData(details[0].contract, "",
                                        _duration_for(min(expiries)), "1 day",
                                        "TRADES", True, 1)
        except Exception:
            continue          # a missing print must never take the cycle down

        series = sorted((d, b) for d, b in ((_bar_day(b), b) for b in bars) if d)
        for exp in expiries:
            # An AM-settled monthly can carry the Saturday in its OSI symbol and
            # a holiday moves the print, so settle on the last session at or
            # before the date rather than demanding an exact hit.
            usable = [(d, b) for d, b in series if d <= exp]
            if not usable:
                continue
            day, bar = usable[-1]
            rec = {"open": _num(bar.open), "high": _num(bar.high),
                   "low": _num(bar.low), "close": _num(bar.close),
                   "bar_date": day, "provisional": day >= today}
            out[(sym, exp)] = rec
            if rec["provisional"]:
                _provisional[(sym, exp)] = (now, rec)
            else:
                cache[f"{sym}|{exp}"] = rec
                dirty = True

    if dirty:
        _save_cache(cache_path, cache)
    return out


# ---------------------------------------------------------------- rollups
def _expiry_label(e):
    e = str(e or "")
    return f"{e[:4]}-{e[4:6]}-{e[6:]}" if len(e) == 8 else (e or "-")


def aggregate_metrics(positions, pnl, greeks, iv_stats, nav=None,
                      nav_currency=None) -> dict:
    """Roll positions up per underlying and per (underlying, expiry).

    Greeks are position-weighted. IB quotes them per share, so every one is
    scaled by qty * multiplier: delta and gamma read as share-equivalents,
    theta as dollars a day, vega as dollars per volatility point.
    ``delta_dollars`` (delta x underlying price) is what makes delta comparable
    across a 7,700 index and a 200 stock.

    Monetary values remain in contract currency. % NAV is only available when
    that currency equals the NAV currency; no implicit FX conversion is made.
    Missing inputs invalidate the affected total rather than becoming zero.
    A daily return needs beginning positions and trading cash flows, so it
    cannot be derived by subtracting daily P&L from current market value.
    """
    buckets: dict[tuple, dict] = {}
    per_ticker: dict[str, dict] = {}

    fields = ("delta", "gamma", "vega", "theta", "delta_dollars", "gamma_dollars",
              "daily_pnl", "unrealized_pnl", "market_value")

    def blank(**kw):
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0,
                "delta_dollars": 0.0, "gamma_dollars": 0.0, "daily_pnl": 0.0, "unrealized_pnl": 0.0,
                "market_value": 0.0, "prior_value": 0.0, "gross_prior_value": 0.0,
                "positions": 0, "contracts": 0.0, "greeks_missing": 0,
                "missing_fields": {field: 0 for field in fields},
                "currencies": set(), "non_live_greeks": 0,
                "estimated_greeks": 0, **kw}

    for p in positions:
        c = p.contract
        if c.secType != "OPT" or not p.position:
            continue
        sym = c.symbol or c.tradingClass
        exp = c.lastTradeDateOrContractMonth
        mult = float(c.multiplier or 100)
        qty = float(p.position)

        tkey = sym
        ekey = (sym, exp)
        per_ticker.setdefault(tkey, blank(underlying=sym))
        buckets.setdefault(ekey, blank(underlying=sym, expiry=exp,
                                       expiry_label=_expiry_label(exp)))

        g = greeks.get(c.conId) or {}
        row = pnl.get((p.account, c.conId)) or {}
        shares = qty * mult
        values = {field: (_num(g.get(field)) * shares
                          if _num(g.get(field)) is not None else None)
                  for field in ("delta", "gamma", "vega", "theta")}
        spot = _num(g.get("und_price"))
        values["delta_dollars"] = (values["delta"] * spot
                                   if values["delta"] is not None
                                   and spot is not None and spot > 0 else None)
        # Gamma per index point reads as nothing on SPX (-0.2 "shares") while
        # a 1% move swings delta $ by six figures. Scale it to the change in
        # delta $ for a 1% move: gamma * S * 1% shares, each worth S.
        values["gamma_dollars"] = (values["gamma"] * spot * spot / 100
                                   if values["gamma"] is not None
                                   and spot is not None and spot > 0 else None)
        values.update({field: _num(row.get(field)) for field in
                       ("daily_pnl", "unrealized_pnl", "market_value")})
        if row.get("position") is not None and row["position"] != qty:
            for field in ("daily_pnl", "unrealized_pnl", "market_value"):
                values[field] = None  # position changed during collection

        for target in (per_ticker[tkey], buckets[ekey]):
            target["currencies"].add(getattr(c, "currency", None) or "unknown")
            if any(values[field] is None for field in
                   ("delta", "gamma", "vega", "theta", "delta_dollars")):
                target["greeks_missing"] += 1
            if g.get("greeks_source") == "prior_close_estimate":
                target["estimated_greeks"] += 1
            elif g and g.get("market_data_type") != 1:
                target["non_live_greeks"] += 1
            for field, value in values.items():
                if value is None:
                    target["missing_fields"][field] += 1
                else:
                    target[field] += value
            target["positions"] += 1
            target["contracts"] += qty

    def finish(d):
        currencies = sorted(d.pop("currencies"))
        d["currency"] = currencies[0] if len(currencies) == 1 else "mixed"
        for field in fields:
            if d["missing_fields"][field] or d["currency"] in ("mixed", "unknown"):
                d[field] = None
        d["prior_value"] = d["gross_prior_value"] = None
        d["daily_pnl_pct"] = None
        d["daily_pnl_pct_nav"] = ((d["daily_pnl"] / nav * 100)
                                  if nav and nav > 0 and d["daily_pnl"] is not None
                                  and d["currency"] == nav_currency else None)
        return d

    tickers = []
    for sym, d in per_ticker.items():
        finish(d)
        d.update({k: v for k, v in (iv_stats.get(sym) or {}).items()})
        tickers.append(d)
    tickers.sort(key=lambda d: -abs(d["daily_pnl"] or 0))

    expiries = [finish(d) for d in buckets.values()]
    expiries.sort(key=lambda d: (d["expiry"] or "", d["underlying"]))
    return {"by_ticker": tickers, "by_expiry": expiries}


def collect_all(ib, accounts=None, snapshot=None, execution_completeness=None) -> dict:
    """One pass: accounts, per-position P&L, Greeks, IV, and the rollups."""
    positions = [p for p in ib.positions()
                 if not accounts or p.account in accounts]
    greeks = collect_greeks(ib, positions)
    unders = {p.contract.symbol for p in positions
              if p.contract.secType == "OPT" and p.contract.symbol}
    iv_stats = collect_iv_stats(ib, unders)
    acct_values = collect_account_values(ib, accounts)
    accounts = set(accounts or acct_values or {p.account for p in positions})
    currencies = {d.get("currency") for d in acct_values.values()}
    nav_currency = next(iter(currencies)) if len(currencies) == 1 else None
    missing_accounts = sorted(set(accounts or []) - set(acct_values))
    nav = (sum(d["NetLiquidation"] for d in acct_values.values())
           if nav_currency and all(d.get("NetLiquidation") is not None
                                   for d in acct_values.values())
           and not missing_accounts else None)
    if snapshot is None:
        import canonical_engine
        snapshot = {'legs': [canonical_engine.normalize_position(p) for p in positions],
                    'fills_today': canonical_engine.collect_fills(ib)}
    universe = daily_pnl.load_universe(snapshot, accounts)
    contracts = list(universe['contracts'].values())
    fx = collect_fx_rates(ib, {c.get('currency') for c in contracts},
                          {d.get('currency') for d in acct_values.values()})
    candidates = [SimpleNamespace(account=c['account'], position=c.get('qty', 0),
                                  contract=SimpleNamespace(conId=c['conId'])) for c in contracts]
    pnl_started = datetime.now(timezone.utc).isoformat()
    pnl, account_pnl = collect_daily_sources(ib, candidates, accounts)
    pnl_ended = datetime.now(timezone.utc).isoformat()
    # Include configured accounts without balances, so missing broker data
    # remains visible in the account selector and daily report.
    balances = {a: acct_values.get(a, {}) for a in accounts}
    daily_accounts = daily_pnl.aggregate_daily(contracts, pnl, balances, account_pnl,
                                              fx, universe['issues'], execution_completeness)
    per_account = {}
    for a, values in balances.items():
        held = [p for p in positions if p.account == a]
        per_account[a] = {
            'accounts': {a: values}, 'nav_total': values.get('NetLiquidation'),
            'nav_currency': values.get('currency'),
            'missing_accounts': [a] if a in missing_accounts else [],
            'radar': assess_expiry_risk(held, greeks),
            'daily': daily_accounts[a],
            **aggregate_metrics(held, pnl, greeks, iv_stats,
                                nav=values.get('NetLiquidation'), nav_currency=values.get('currency')),
        }
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "accounts": acct_values,
        "nav_total": nav,
        "nav_currency": nav_currency,
        "missing_accounts": missing_accounts,
        "per_account": per_account,
        "daily_accounts": daily_accounts,
        "pnl_started_at": pnl_started, "pnl_ended_at": pnl_ended,
        "trade_date": universe['trade_date'],
        "scope": "Open options in configured accounts; excludes stock/ETF holdings and fully closed positions",
        "iv": iv_stats,
        "radar": assess_expiry_risk(positions, greeks),
        **aggregate_metrics(positions, pnl, greeks, iv_stats, nav=nav,
                            nav_currency=nav_currency),
    }
