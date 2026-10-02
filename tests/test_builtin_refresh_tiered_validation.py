import unittest
from unittest.mock import patch


class _FakeRefreshDB:
    def __init__(self):
        self._proposal_id = 0
        self._proposal = None
        self.metrics = []
        self._registry = [
            {
                "name": "S&P 500 Top 100",
                "source": "built-in",
                "enabled": True,
                "source_mode": "objective",
                "target_size": 3,
                "tickers": "AAA,BBB,CCC",
            }
        ]
        self._watchlists = [
            {"id": 1, "name": "S&P 500 Top 100", "tickers": "AAA,BBB,CCC"}
        ]

    def _builtin_watchlist_registry(self):
        return list(self._registry)

    def get_builtin_watchlists(self):
        return list(self._watchlists)

    def get_ticker_metadata_bulk(self, tickers):
        return {}

    def save_ticker_metadata(self, **kwargs):
        return None

    def create_builtin_refresh_proposal(self, items, source="manual", metadata=None):
        self._proposal_id += 1
        self._proposal = {
            "id": self._proposal_id,
            "status": "proposed",
            "source": source,
            "metadata": metadata or {},
            "summary": {},
            "items": items,
        }
        return self._proposal_id

    def get_builtin_refresh_proposal(self, proposal_id):
        if self._proposal and self._proposal["id"] == proposal_id:
            return self._proposal
        return None

    def record_runtime_metric(self, metric_key, duration_seconds, context=None):
        self.metrics.append(
            {
                "metric_key": metric_key,
                "duration_seconds": float(duration_seconds),
                "context": context or {},
            }
        )
        return len(self.metrics)


class TestTieredValidator(unittest.TestCase):
    def test_tier1_membership_validates_without_fallback(self):
        from tradingagents.screening.builtin_refresh import _validate_symbols_tiered

        with patch(
            "tradingagents.screening.builtin_refresh.get_active_us_symbols",
            return_value={"AAPL", "MSFT"},
        ), patch(
            "tradingagents.screening.builtin_refresh.get_us_symbol_set",
            return_value=set(),
        ), patch(
            "tradingagents.screening.builtin_refresh._fetch_ticker_snapshot_limited"
        ) as tier3:
            valid, invalid, _, stats = _validate_symbols_tiered(["AAPL", "MSFT"])

        self.assertEqual(valid, ["AAPL", "MSFT"])
        self.assertEqual(invalid, [])
        self.assertEqual(stats["tier1_validated"], 2)
        self.assertEqual(stats["tier2_validated"], 0)
        self.assertEqual(stats["tier3_validated"], 0)
        tier3.assert_not_called()

    def test_tier2_activates_when_tier1_is_empty(self):
        from tradingagents.screening.builtin_refresh import _validate_symbols_tiered

        with patch(
            "tradingagents.screening.builtin_refresh.get_active_us_symbols",
            return_value=set(),
        ), patch(
            "tradingagents.screening.builtin_refresh.get_us_symbol_set",
            return_value={"AAPL"},
        ), patch(
            "tradingagents.screening.builtin_refresh._fetch_ticker_snapshot_limited"
        ) as tier3:
            valid, invalid, _, stats = _validate_symbols_tiered(["AAPL"])

        self.assertEqual(valid, ["AAPL"])
        self.assertEqual(invalid, [])
        self.assertEqual(stats["tier1_validated"], 0)
        self.assertEqual(stats["tier2_validated"], 1)
        self.assertEqual(stats["tier3_validated"], 0)
        tier3.assert_not_called()

    def test_tier3_only_runs_for_leftovers_and_chunks(self):
        from tradingagents.screening.builtin_refresh import _validate_symbols_tiered

        symbols = [f"T{i:03d}" for i in range(251)]

        def _fake_tier3(sym, retries=1):
            return {"symbol": sym, "valid": sym.endswith("0")}

        with patch(
            "tradingagents.screening.builtin_refresh.get_active_us_symbols",
            return_value=set(),
        ), patch(
            "tradingagents.screening.builtin_refresh.get_us_symbol_set",
            return_value=set(),
        ), patch(
            "tradingagents.screening.builtin_refresh._fetch_ticker_snapshot_limited",
            side_effect=_fake_tier3,
        ) as tier3:
            valid, invalid, _, stats = _validate_symbols_tiered(
                symbols,
                max_workers=2,
                chunk_size=250,
            )

        self.assertEqual(len(valid), 26)
        self.assertEqual(len(invalid), 225)
        self.assertEqual(stats["tier3_attempted"], 251)
        self.assertEqual(stats["tier3_validated"], 26)
        self.assertEqual(tier3.call_count, 251)


class TestRefreshTelemetryAndBehavior(unittest.TestCase):
    def test_curated_branch_keeps_expected_output_and_records_metrics(self):
        from tradingagents.screening.builtin_refresh import build_refresh_proposal

        db = _FakeRefreshDB()
        baseline_order = ["AAA", "BBB", "CCC"]
        cap_map = {"AAA": 300.0, "BBB": 200.0, "CCC": 100.0}

        with patch(
            "tradingagents.screening.builtin_refresh._fetch_index_symbols",
            return_value=(baseline_order, []),
        ), patch(
            "tradingagents.screening.builtin_refresh._validate_symbols_tiered",
            return_value=(
                baseline_order,
                [],
                {},
                {
                    "tier1_validated": 3,
                    "tier2_validated": 0,
                    "tier3_validated": 0,
                    "tier3_attempted": 0,
                },
            ),
        ), patch(
            "tradingagents.screening.builtin_refresh._hydrate_market_caps",
            return_value=(cap_map, 0),
        ):
            proposal = build_refresh_proposal(db=db, source="test")

        self.assertEqual(proposal["items"][0]["new_tickers"], baseline_order)
        metric_keys = {m["metric_key"] for m in db.metrics}
        self.assertIn("builtin_refresh_duration_seconds", metric_keys)
        self.assertIn("builtin_refresh_invalid_ratio", metric_keys)
        self.assertIn("builtin_refresh_null_market_cap_count", metric_keys)


if __name__ == "__main__":
    unittest.main()
