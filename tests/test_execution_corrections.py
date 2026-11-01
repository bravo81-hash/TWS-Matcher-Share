"""IBKR execution corrections: a later execId version replaces the original."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

import canonical_engine as eng
import flex_export


def fill(exec_id, side="BOT", shares=1.0, account="U1", strike=7590.0,
         time="2026-10-07 19:36:56+00:00"):
    return {"account": account, "execId": exec_id, "side": side, "shares": shares,
            "price": 6.62, "time": time, "orderId": 7, "secType": "OPT",
            "underlying": "SPX", "tradingClass": "SPX", "expiry": "20261015",
            "strike": strike, "right": "P", "multiplier": 100.0, "conId": 1}


class ExecutionCorrectionTests(unittest.TestCase):
    def test_zero_share_correction_cancels_the_original(self):
        effective, cancelled = eng.apply_execution_corrections([
            fill("a.b.03.01"),
            fill("a.b.03.02", shares=0.0, time="2026-10-07 20:19:35+00:00"),
            fill("c.d.01.01", side="SLD")])
        self.assertEqual([f["execId"] for f in effective], ["c.d.01.01"])
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["execId"], "a.b.03.01")
        self.assertEqual(cancelled[0]["shares"], 1.0)
        self.assertEqual(cancelled[0]["cancelled_at"], "2026-10-07 20:19:35+00:00")

    def test_non_zero_correction_replaces_without_double_counting(self):
        effective, cancelled = eng.apply_execution_corrections([
            fill("a.b.03.01", shares=2.0), fill("a.b.03.02", shares=1.0)])
        self.assertEqual([(f["execId"], f["shares"]) for f in effective],
                         [("a.b.03.02", 1.0)])
        self.assertEqual(cancelled, [])

    def test_correction_seen_only_without_original_is_still_cancelled(self):
        effective, cancelled = eng.apply_execution_corrections(
            [fill("a.b.03.02", shares=0.0)])
        self.assertEqual(effective, [])
        self.assertEqual(len(cancelled), 1)

    def test_same_exec_prefix_in_other_account_is_independent(self):
        effective, _ = eng.apply_execution_corrections(
            [fill("a.b.03.01", account="U1"), fill("a.b.03.02", account="U2", shares=0)])
        self.assertEqual([f["account"] for f in effective], ["U1"])

    def test_journal_drops_busted_trade_from_import_and_completeness(self):
        # Position never changed: the 19:36 order was busted at 20:19.
        legs = [{"account": "U1", "secType": "OPT", "tradingClass": "SPX",
                 "expiry": "20261015", "strike": 7590.0, "right": "P", "qty": 1.0}]
        now = datetime(2026, 10, 7, 20, 30, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "journal.json")
            first = {"captured_at": now.isoformat(), "legs": legs,
                     "managed_accounts": ["U1"], "fills_today": [fill("a.b.03.01")]}
            flex_export.update_execution_journal(
                {**first, "legs": [{**legs[0], "qty": 2.0}]}, path, now)
            # TWS now only reports the correction; the original is in the journal.
            snap, completeness = flex_export.update_execution_journal(
                {**first, "fills_today": [fill("a.b.03.02", shares=0.0,
                                                time="2026-10-07 20:19:35+00:00")]},
                path, now)
        self.assertEqual(snap["fills_today"], [])
        self.assertEqual([f["execId"] for f in snap["cancelled_fills"]], ["a.b.03.01"])
        self.assertEqual(completeness["U1"]["status"], flex_export.COMPLETE)

    def test_unlabelled_execution_time_is_detected(self):
        import ib_async.decoder as decoder
        eng.take_unlabelled_exec_time_count()
        decoder.parseIBDatetime("20261007 14:59:57 US/Central")
        self.assertEqual(eng.take_unlabelled_exec_time_count(), 0)
        decoder.parseIBDatetime("20261007 19:27:54")
        self.assertEqual(eng.take_unlabelled_exec_time_count(), 1)
        self.assertEqual(eng.take_unlabelled_exec_time_count(), 0)


if __name__ == "__main__":
    unittest.main()
