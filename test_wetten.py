import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import wetten_core as w


class TestBettingJournal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original = w.DB_PATH
        w.DB_PATH = Path(self.tmp.name) / "journal.sqlite3"
        w.init_db()

    def tearDown(self):
        w.DB_PATH = self.original
        self.tmp.cleanup()

    def entry(self, odds="2.40", legs=None):
        return {
            "bookmaker": "bet365", "placed_at": "2026-10-25T12:30:00+01:00",
            "stake": "12.50", "legs": legs or [
                {"sport": "NFL", "home": "Test Home", "away": "Test Away",
                 "market": "moneyline", "selection": "home", "odds": odds}
            ]
        }

    def test_create_persists_in_sqlite_with_exact_cents(self):
        number = w.create_ticket(self.entry())
        ticket = w.view_data()["tickets"][0]
        self.assertEqual(ticket["id"], number)
        self.assertEqual(ticket["stake_cents"], 1250)
        self.assertEqual(ticket["potential_cents"], 3000)
        self.assertEqual(ticket["status"], "open")
        self.assertEqual(len(ticket["legs"]), 1)

    def test_invalid_bookmaker_market_and_precision_fail(self):
        record = self.entry()
        record["bookmaker"] = "Fake Book"
        with self.assertRaises(ValueError): w.create_ticket(record)
        record = self.entry()
        record["stake"] = "5.001"
        with self.assertRaises(ValueError): w.create_ticket(record)
        record = self.entry()
        record["legs"][0]["market"] = "unknown"
        with self.assertRaises(ValueError): w.create_ticket(record)

    def test_spread_push_and_win(self):
        home = dict(market="spread", selection="home", line="-3", period="full_game")
        away = dict(market="spread", selection="away", line="+3", period="full_game")
        self.assertEqual(w.evaluate(home, 24, 21), "push")
        self.assertEqual(w.evaluate(away, 24, 21), "push")
        self.assertEqual(w.evaluate(home, 28, 21), "won")
        self.assertEqual(w.evaluate(away, 28, 21), "lost")

    def test_football_markets(self):
        self.assertEqual(w.evaluate({"market": "1x2", "selection": "draw", "period": "full_game"}, 1, 1), "won")
        self.assertEqual(w.evaluate({"market": "dnb", "selection": "home", "period": "full_game"}, 1, 1), "push")
        self.assertEqual(w.evaluate({"market": "double_chance", "selection": "1x", "period": "full_game"}, 3, 0), "won")
        self.assertEqual(w.evaluate({"market": "btts", "selection": "yes", "period": "full_game"}, 2, 0), "lost")
        self.assertEqual(w.evaluate({"market": "total", "selection": "over", "line": "2.5", "period": "full_game"}, 2, 1), "won")

    def test_multi_legs_one_push_recalculates_payout(self):
        legs = [
            {"sport":"NFL","home":"One","away":"Two","market":"moneyline","selection":"home","odds":"2.00"},
            {"sport":"NFL","home":"Three","away":"Four","market":"spread","selection":"home","line":"-3","odds":"1.90"},
        ]
        number = w.create_ticket(self.entry(legs=legs))
        with w.connection() as con:
            ids = [r["id"] for r in con.execute("SELECT id FROM legs WHERE ticket_id=? ORDER BY id", (number,))]
            con.execute("UPDATE legs SET status='won' WHERE id=?", (ids[0],))
            con.execute("UPDATE legs SET status='push' WHERE id=?", (ids[1],))
            w.recompute(con, number)
        row = w.view_data()["tickets"][0]
        self.assertEqual(row["status"], "won")
        self.assertEqual(row["payout_cents"], 2500)

    def test_manual_override_cannot_be_overwritten(self):
        number = w.create_ticket(self.entry())
        w.manual_settle({"id":number,"status":"lost","reason":"Confirmed bookmaker outcome"})
        with w.connection() as con:
            con.execute("UPDATE legs SET status='won' WHERE ticket_id=?", (number,))
            w.recompute(con, number)
        self.assertEqual(w.view_data()["tickets"][0]["status"], "lost")

    def test_html_must_show_final_and_unique_score(self):
        html = "<section>Test Home 24 - 21 Test Away <strong>FINAL</strong></section>"
        self.assertEqual(w.extract_final_score(html, "Test Home", "Test Away"), (24, 21))
        self.assertIsNone(w.extract_final_score(html.replace("FINAL","Q3"), "Test Home", "Test Away"))
        self.assertIsNone(w.extract_final_score(html+html, "Test Home", "Test Away"))
        hidden = "<script>Test Home 24 - 21 Test Away FINAL</script>"
        self.assertIsNone(w.extract_final_score(hidden, "Test Home", "Test Away"))

    def test_no_external_api_or_unapproved_host_access(self):
        with patch("wetten_core.build_opener", side_effect=AssertionError("must not fetch")):
            result, reason = w.html_result("https://private.example/scores", "A", "B", set())
        self.assertIsNone(result)
        self.assertIn("freigegeben", reason)
        with patch.dict(os.environ, {"WETTEN_HTML_ALLOW_HOSTS": ""}):
            r = w.check_once()
        self.assertEqual(r["result"], "disabled_no_approved_html_sources")

    def test_delete_is_soft_and_audited(self):
        number = w.create_ticket(self.entry())
        w.delete_ticket({"id":number})
        self.assertEqual(w.view_data()["tickets"], [])
        with w.connection() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit WHERE ticket_id=?", (number,)).fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
