"""Post-close watchlist policy: cadence, exclude, priority order."""
import copy
import unittest
from unittest.mock import patch

from tradingagents.default_config import DEFAULT_CONFIG


class TestSchedulerPostclosePolicy(unittest.TestCase):
    def _scheduler(self):
        from webapp.screening_scheduler import ScreeningScheduler

        cfg = copy.deepcopy(DEFAULT_CONFIG)
        return ScreeningScheduler(db_path=":memory:", config=cfg)

    def test_weekly_lists_skipped_on_non_friday(self):
        from datetime import datetime

        scheduler = self._scheduler()
        fake = [
            {"id": 1, "name": "NASDAQ 100", "source": "built-in", "tickers": "AAPL"},
            {"id": 66, "name": "Healthcare & Life Sciences", "source": "built-in", "tickers": "UNH"},
            {"id": 43, "name": "S&P 500", "source": "built-in", "tickers": "AAPL,MSFT"},
        ]
        with patch("webapp.screening_scheduler.get_db") as mock_db:
            mock_db.return_value.get_watchlists.return_value = fake
            with patch("webapp.screening_scheduler._now_et") as mock_et:
                mock_et.return_value = datetime(2026, 9, 29, 12, 0, 0)  # Tuesday
                names = [
                    w["name"]
                    for w in scheduler._get_scannable_watchlists(apply_postclose_policy=True)
                ]
        self.assertIn("NASDAQ 100", names)
        self.assertIn("Healthcare & Life Sciences", names)
        self.assertNotIn("S&P 500", names)

    def test_weekly_lists_included_on_friday(self):
        from datetime import datetime

        scheduler = self._scheduler()
        fake = [
            {"id": 43, "name": "S&P 500", "source": "built-in", "tickers": "AAPL"},
        ]
        with patch("webapp.screening_scheduler.get_db") as mock_db:
            mock_db.return_value.get_watchlists.return_value = fake
            with patch("webapp.screening_scheduler._now_et") as mock_et:
                mock_et.return_value = datetime(2026, 10, 2, 12, 0, 0)  # Friday
                names = [
                    w["name"]
                    for w in scheduler._get_scannable_watchlists(apply_postclose_policy=True)
                ]
        self.assertEqual(names, ["S&P 500"])

    def test_priority_order_puts_healthcare_first(self):
        scheduler = self._scheduler()
        rows = [
            {"id": 1, "name": "Sector ETFs"},
            {"id": 66, "name": "Healthcare & Life Sciences"},
            {"id": 2, "name": "NASDAQ 100"},
        ]
        ordered = scheduler._sort_watchlists_for_scheduled_scan(rows)
        self.assertEqual(ordered[0]["name"], "Healthcare & Life Sciences")

    def test_my_watchlist_included_when_user_source_and_has_tickers(self):
        from datetime import datetime

        scheduler = self._scheduler()
        fake = [
            {"id": 5, "name": "My Watchlist", "source": "user", "tickers": "RKLB,ASTS"},
            {"id": 9, "name": "Other custom", "source": "user", "tickers": "AAPL"},
        ]
        with patch("webapp.screening_scheduler.get_db") as mock_db:
            mock_db.return_value.get_watchlists.return_value = fake
            with patch("webapp.screening_scheduler._now_et") as mock_et:
                mock_et.return_value = datetime(2026, 9, 29, 12, 0, 0)
                names = [
                    w["name"]
                    for w in scheduler._get_scannable_watchlists(apply_postclose_policy=True)
                ]
        self.assertEqual(names, ["My Watchlist"])

    def test_my_watchlist_priority_first(self):
        scheduler = self._scheduler()
        rows = [
            {"id": 66, "name": "Healthcare & Life Sciences"},
            {"id": 5, "name": "My Watchlist"},
        ]
        ordered = scheduler._sort_watchlists_for_scheduled_scan(rows)
        self.assertEqual(ordered[0]["name"], "My Watchlist")

    def test_ai_focus_list_is_excluded_by_default(self):
        scheduler = self._scheduler()
        self.assertFalse(
            scheduler._is_watchlist_eligible_for_scheduled_scan("AI & AI Infrastructure", "tuesday")
        )
        self.assertFalse(
            scheduler._is_watchlist_eligible_for_scheduled_scan("AI & AI Infrastructure", "friday")
        )

    def test_exclude_list(self):
        scheduler = self._scheduler()
        scheduler._config.setdefault("postclose", {})["exclude_watchlist_names"] = ["ADRs - Top"]
        self.assertFalse(
            scheduler._is_watchlist_eligible_for_scheduled_scan("ADRs - Top", "tuesday")
        )
        self.assertTrue(
            scheduler._is_watchlist_eligible_for_scheduled_scan("NASDAQ 100", "tuesday")
        )


if __name__ == "__main__":
    unittest.main()
