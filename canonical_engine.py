#!/usr/bin/env python3
"""
Canonical Position Engine  —  IBKR source of truth
Step 1 of the TWS / ONE / OptionStrat reconciliation system.

Connects to TWS, pulls the authoritative position set (and today's fills)
across ALL accounts, normalizes to a single canonical schema, prints a
readable per-account table, and writes canonical_positions.json for the
downstream consumers (OptionStrat URL generator + ONE .mdb diff).

This script also doubles as a TWS-connectivity + account-inventory probe:
run it once and paste the console output back.

SETUP
    pip install ib_async
    In TWS: Global Configuration > API > Settings
        [x] Enable ActiveX and Socket Clients
        Socket port == PORT below          (7496 = live, 7497 = paper)
        Add 127.0.0.1 to Trusted IPs       (stops the connect pop-up)
    Use a CLIENT_ID different from the one ONE uses (ONE often defaults to 0/1).

NOTE
    avg_price is derived from IBKR avgCost / multiplier (premium per share),
    so it is apples-to-apples with ONE/OptionStrat. Depending on account config,
    avgCost may fold in commissions, so allow a small tolerance when diffing
    fill prices. The fills_today[].price field is the clean per-share fill.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from ib_async import IB, Position, Contract

import snapshots

# ----------------------------------------------------------------- config
HOST = "127.0.0.1"   # TWS on THIS laptop. If TWS is on the Mac, use its LAN IP.
PORT = 7496          # 7496 = TWS live, 7497 = TWS paper
CLIENT_ID = 17       # dedicated id; MUST differ from ONE's TWS client id
CONNECT_TIMEOUT = 15
OUTPUT_JSON = "canonical_positions.json"
# -------------------------------------------------------------------------


def _f(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _pretty_expiry(exp: str | None) -> str:
    if exp and len(exp) == 8:
        return f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
    return exp or "?"


def normalize_position(p: Position) -> dict:
    c: Contract = p.contract
    mult = _f(c.multiplier, 1.0) or 1.0
    avg_cost = _f(p.avgCost)                      # IBKR: per-contract incl. multiplier (options)
    px_per_share = avg_cost / mult if mult else avg_cost
    return {
        "account": p.account,
        "conId": c.conId,
        "secType": c.secType,
        "underlying": c.symbol,
        "tradingClass": c.tradingClass or c.symbol,   # SPX vs SPXW matters
        "expiry": c.lastTradeDateOrContractMonth or None,
        "strike": _f(c.strike) if c.strike else None,
        "right": c.right or None,                     # 'C' / 'P'
        "multiplier": mult,
        "qty": _f(p.position),                        # signed: negative = short
        "avg_price": round(px_per_share, 4),          # per-share (compare vs ONE/OptionStrat)
        "avg_cost_raw": round(avg_cost, 2),           # IBKR raw (incl. multiplier)
        "localSymbol": c.localSymbol or None,
        "currency": c.currency,
        "exchange": c.exchange or c.primaryExchange or None,
    }


# ---------------------------------------------------------------- exec times
# TWS normally stamps execution times with a zone ("20261007 14:59:57
# US/Central"). A client that connects while TWS is still starting up can get
# bare UTC wall-clock times instead, which ib_async reads as *system local*
# time -- every fill then lands hours early outside UTC. ib_async's decoder
# uses this parser for execution times only, so counting unlabelled results
# there flags a session whose fill times cannot be trusted.
_unlabelled_exec_times = [0]


def _install_exec_time_guard():
    import ib_async.decoder as decoder
    parse = decoder.parseIBDatetime
    if getattr(parse, "_tws_matcher_guard", False):
        return

    def guarded(s):
        t = parse(s)
        if isinstance(t, datetime) and t.tzinfo is None:
            _unlabelled_exec_times[0] += 1
        return t

    guarded._tws_matcher_guard = True
    decoder.parseIBDatetime = guarded


_install_exec_time_guard()


def take_unlabelled_exec_time_count() -> int:
    """Unlabelled execution times seen since the last call (then resets)."""
    n, _unlabelled_exec_times[0] = _unlabelled_exec_times[0], 0
    return n


def _correction_parts(exec_id):
    """'0000f711.6cd1e4d8.02.01' -> ('0000f711.6cd1e4d8.02', 1), else None."""
    base, _, version = str(exec_id or "").strip().rpartition(".")
    return (base, int(version)) if base and version.isdigit() else None


def apply_execution_corrections(fills: list[dict]):
    """Return ``(effective_fills, cancelled_fills)``.

    IBKR reports a correction as a new execution whose execId keeps the
    prefix and increments the last segment (``...02.01`` -> ``...02.02``).
    The latest version replaces every earlier one; a correction to 0 shares
    means the execution was cancelled (busted) and never changed a position.
    Treating the original and its correction as two separate trades books a
    cancelled trade as real -- into reconciliation and into ONE import files.
    ``cancelled_fills`` holds the original executions that were cancelled.
    """
    latest, versions = {}, defaultdict(list)
    for fill in fills or []:
        parts = _correction_parts(fill.get("execId"))
        if not parts:
            continue
        key = (str(fill.get("account") or ""), parts[0])
        versions[key].append((parts[1], fill))
        if key not in latest or parts[1] > latest[key][0]:
            latest[key] = (parts[1], fill)
    effective, cancelled = [], []
    for fill in fills or []:
        parts = _correction_parts(fill.get("execId"))
        if not parts:
            effective.append(fill)
            continue
        key = (str(fill.get("account") or ""), parts[0])
        version, final = latest[key]
        if parts[1] != version:
            continue                      # superseded by a later correction
        if abs(float(final.get("shares") or 0)) > 1e-9:
            effective.append(fill)
        else:
            originals = [f for v, f in sorted(versions[key], key=lambda x: x[0])
                         if abs(float(f.get("shares") or 0)) > 1e-9]
            cancelled.append({**(originals[0] if originals else final),
                              "cancelled_at": final.get("time"),
                              "correction_execId": final.get("execId")})
    return effective, cancelled


def collect_fills(ib: IB) -> list[dict]:
    out: list[dict] = []
    fills = []
    try:
        # Refresh executions explicitly on every cycle. The local cache alone
        # can be incomplete after a reconnect, especially for FA allocations.
        fills.extend(ib.reqExecutions() or [])
    except Exception:
        pass
    try:
        fills.extend(ib.fills() or [])
    except Exception:
        pass

    # reqExecutions() also updates the local cache, so the lists usually
    # overlap. Preserve one copy of each broker execution.
    unique = {}
    for fill in fills:
        e = fill.execution
        exec_id = str(getattr(e, "execId", "") or "")
        key = ((str(getattr(e, "acctNumber", "") or ""), exec_id)
               if exec_id else repr((
            getattr(e, "acctNumber", None), getattr(e, "time", None),
            getattr(e, "orderId", None), getattr(e, "side", None),
            getattr(e, "shares", None), getattr(e, "price", None),
            getattr(fill.contract, "conId", None),
        )))
        unique[key] = fill

    for fill in unique.values():
        c, e = fill.contract, fill.execution
        cr = getattr(fill, "commissionReport", None)
        commission = _f(getattr(cr, "commission", None)) if cr else None
        out.append({
            "account": getattr(e, "acctNumber", None),
            "conId": c.conId,
            "time": str(getattr(e, "time", "")),
            "secType": c.secType,
            "underlying": c.symbol,
            "tradingClass": c.tradingClass or c.symbol,
            "expiry": c.lastTradeDateOrContractMonth or None,
            "strike": _f(c.strike) if c.strike else None,
            "right": c.right or None,
            "multiplier": _f(c.multiplier, 1.0) or 1.0,
            "currency": c.currency,
            "localSymbol": c.localSymbol or None,
            "exchange": c.exchange or c.primaryExchange or None,
            "side": getattr(e, "side", None),         # BOT / SLD
            "shares": _f(getattr(e, "shares", 0)),
            "price": _f(getattr(e, "price", 0)),      # clean per-share fill price
            "commission": commission,                 # signed; None until report arrives
            "commission_currency": getattr(cr, "currency", None) if cr else None,
            "execId": getattr(e, "execId", None),
            "orderId": getattr(e, "orderId", None),
            "permId": getattr(e, "permId", None),
        })
    return out


def leg_label(d: dict) -> str:
    if d.get("right"):
        strike = d.get("strike")
        strike_s = f"{strike:g}" if strike is not None else "?"
        return f'{d["tradingClass"]} {_pretty_expiry(d["expiry"])} {strike_s}{d["right"]}'
    return f'{d["underlying"]} ({d.get("secType", "")})'.strip()


def print_table(legs: list[dict], fills: list[dict], accounts: list[str]) -> None:
    by_acct: dict[str, list[dict]] = {}
    for leg in legs:
        by_acct.setdefault(leg["account"], []).append(leg)

    print("\n" + "=" * 74)
    print(f"CANONICAL POSITIONS (IBKR truth)   {datetime.now().isoformat(timespec='seconds')}")
    print(f"Managed accounts: {', '.join(accounts) or '(none)'}")
    print("=" * 74)

    for acct in accounts:
        rows = by_acct.get(acct, [])
        print(f"\n[{acct}]  {len(rows)} legs")
        if not rows:
            print("    (no open positions)")
            continue
        rows.sort(key=lambda r: (r["underlying"], r["expiry"] or "",
                                 r["strike"] or 0, r["right"] or ""))
        print(f'    {"INSTRUMENT":<30}{"QTY":>7}{"AVG PX":>12}')
        for r in rows:
            print(f'    {leg_label(r):<30}{r["qty"]:>7.0f}{r["avg_price"]:>12.4f}')

    for acct in sorted(set(by_acct) - set(accounts)):
        print(f"\n[{acct}] (NOT in managedAccounts — separate login?)  {len(by_acct[acct])} legs")

    print(f"\nToday's fills captured: {len(fills)}")
    for fl in fills:
        print(f'    {fl["time"]}  {str(fl["side"]):<3} {fl["shares"]:>4.0f} '
              f'{leg_label(fl):<30} @ {fl["price"]:.4f}')
    print("=" * 74 + "\n")


def build_snapshot(ib: IB) -> dict:
    """Capture the canonical IBKR snapshot from an already-connected IB.

    Reusable by the daemon/dashboard (which keeps one persistent connection),
    as well as by main() below. Does NOT connect or disconnect.
    """
    ib.sleep(0.5)
    accounts = list(ib.managedAccounts())
    ib.reqPositions()
    ib.sleep(1.0)                                     # let positions populate
    legs = [normalize_position(p) for p in ib.positions()]
    fills, cancelled = apply_execution_corrections(collect_fills(ib))
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "source": "IBKR/TWS",
        "managed_accounts": accounts,
        "legs": legs,
        "fills_today": fills,
        "cancelled_fills": cancelled,
    }


def main() -> None:
    ib = IB()
    print(f"Connecting to TWS at {HOST}:{PORT} (clientId={CLIENT_ID}) ...")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=CONNECT_TIMEOUT)
    except Exception as exc:
        print(f"\nCONNECT FAILED: {exc}")
        print("Checklist: TWS running & logged in; API socket clients enabled; "
              "PORT matches TWS; CLIENT_ID free; 127.0.0.1 trusted.")
        return

    try:
        snapshot = build_snapshot(ib)
        print_table(snapshot["legs"], snapshot["fills_today"],
                    snapshot["managed_accounts"])
        snapshots.write_json_atomic(OUTPUT_JSON, snapshot)
        print(f"Wrote {OUTPUT_JSON}  "
              f"({len(snapshot['legs'])} legs, {len(snapshot['fills_today'])} fills)")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
