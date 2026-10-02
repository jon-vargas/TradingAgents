import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch


class _FakeBuiltinRefreshDB:
    def __init__(self):
        self._proposal_id = 0
        self._proposal_items = []
        self._proposal_metadata = {}
        self._registry = [
            {
                "name": "Russell 2000 Top 100",
                "source": "built-in",
                "enabled": True,
                "source_mode": "objective",
                "target_size": 3,
                "tickers": "AAA,BBB,CCC",
            }
        ]
        self._watchlists = [
            {"id": 1, "name": "Russell 2000 Top 100", "tickers": "AAA,BBB,CCC"}
        ]

    def _builtin_watchlist_registry(self):
        return list(self._registry)

    def get_builtin_watchlists(self):
        return list(self._watchlists)

    def create_builtin_refresh_proposal(self, items, source="manual", metadata=None):
        self._proposal_id += 1
        self._proposal_items = items
        self._proposal_metadata = metadata or {}
        return self._proposal_id

    def get_builtin_refresh_proposal(self, proposal_id):
        if proposal_id != self._proposal_id:
            return None
        return {
            "id": proposal_id,
            "status": "proposed",
            "source": "test",
            "metadata": self._proposal_metadata,
            "summary": {},
            "items": self._proposal_items,
        }


class TestBuiltinRefreshHardening(unittest.TestCase):
    def test_stabilize_candidate_caps_replacements(self):
        from tradingagents.screening.builtin_refresh import _stabilize_objective_candidate

        old = ["A", "B", "C", "D", "E"]
        universe = ["A", "B", "C", "N1", "N2", "N3", "N4"]
        # target=5 and max_replacements=2 => keep at least 3 incumbents.
        candidate = _stabilize_objective_candidate(old, universe, target_size=5, max_replacements=2)
        self.assertEqual(len(candidate), 5)
        old_overlap = len(set(candidate) & set(old))
        self.assertGreaterEqual(old_overlap, 3)
        self.assertLessEqual(len(set(candidate) - set(old)), 2)

    def test_russell_sparse_validation_retains_existing_when_fallback_disabled(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.screening.builtin_refresh import build_refresh_proposal

        db = _FakeBuiltinRefreshDB()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        refresh_cfg = cfg["screening"]["scheduler"]["builtin_refresh"]
        refresh_cfg["allow_sparse_baseline_fallback"] = False
        refresh_cfg["sparse_validation_min_ratio"] = 0.90
        refresh_cfg["validation_workers"] = 2
        refresh_cfg["min_size_by_list"]["Russell 2000 Top 100"] = 1

        with patch(
            "tradingagents.screening.builtin_refresh._validate_symbols_tiered",
            return_value=(["AAA"], ["BBB", "CCC"], {}, {"tier1_validated": 1, "tier2_validated": 0, "tier3_validated": 0, "tier3_attempted": 0}),
        ), patch(
            "tradingagents.screening.builtin_refresh._hydrate_market_caps",
            return_value=({"AAA": 100.0}, 0),
        ):
            proposal = build_refresh_proposal(db=db, config=cfg, source="test")

        self.assertEqual(proposal["id"], 1)
        item = proposal["items"][0]
        self.assertEqual(item["watchlist_name"], "Russell 2000 Top 100")
        self.assertEqual(item["new_tickers"], ["AAA", "BBB", "CCC"])
        self.assertTrue(
            any("sparse validation guard" in w and "retained existing list" in w for w in item["warnings"])
        )

    def test_apply_requires_force_for_high_risk_proposals(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="refresh_hardening_", suffix=".db")
        os.close(fd)
        try:
            db = ResearchDatabase(db_path)
            watchlist_id = int(
                db.create_watchlist(
                    name="Refresh Guard Test",
                    tickers="AAA,BBB",
                    description="test",
                )
            )
            with db._connect() as conn:
                conn.execute(
                    "UPDATE watchlists SET source = 'built-in' WHERE id = ?",
                    (watchlist_id,),
                )
                conn.commit()

            proposal_id = db.create_builtin_refresh_proposal(
                items=[
                    {
                        "watchlist_id": watchlist_id,
                        "watchlist_name": "Refresh Guard Test",
                        "old_tickers": ["AAA", "BBB"],
                        "new_tickers": ["CCC", "DDD"],
                        "warnings": ["high churn warning (100.0% > 30.0%)"],
                    }
                ],
                source="test",
                metadata={"warnings": ["Refresh Guard Test: high churn warning (100.0% > 30.0%)"]},
            )

            guard_cfg = {
                "enforce_high_risk_apply": True,
                "require_force_reason": True,
                "force_apply_churn_pct": 0.40,
            }
            blocked = db.apply_builtin_refresh_proposal(proposal_id, guard_config=guard_cfg)
            self.assertFalse(blocked["applied"])
            self.assertTrue(blocked.get("requires_force_apply"))

            no_reason = db.apply_builtin_refresh_proposal(
                proposal_id,
                force_apply=True,
                force_reason="",
                guard_config=guard_cfg,
            )
            self.assertFalse(no_reason["applied"])
            self.assertEqual(no_reason["message"], "Force apply reason required")

            applied = db.apply_builtin_refresh_proposal(
                proposal_id,
                force_apply=True,
                force_reason="Index rebalance and stale baseline cleanup",
                guard_config=guard_cfg,
            )
            self.assertTrue(applied["applied"])
            self.assertTrue(applied["forced_apply"])
            self.assertIn("risk_items", applied)
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass


class TestRussell2000HoldingsFallback(unittest.TestCase):
    def test_etf_csv_rejects_html_bot_page(self):
        from tradingagents.screening import builtin_refresh

        html_body = b"<!DOCTYPE html><html><head><title>blocked</title></head></html>"
        with patch("tradingagents.screening.builtin_refresh.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = html_body
            symbols, warnings = builtin_refresh._fetch_etf_holdings_symbols("https://example.com/holdings.csv")
        self.assertEqual(symbols, [])
        self.assertTrue(any("HTML instead of CSV" in w for w in warnings))

    def test_vanguard_holdings_json_parser(self):
        from tradingagents.screening import builtin_refresh

        payload = {
            "size": 3,
            "fund": {
                "entity": [
                    {"ticker": "BE"},
                    {"ticker": "CRDO"},
                    {"ticker": ""},
                ]
            },
        }
        with patch("tradingagents.screening.builtin_refresh.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
            symbols, warnings = builtin_refresh._fetch_vanguard_etf_holdings_symbols("https://example.com/vtwo")
        self.assertEqual(symbols, ["BE", "CRDO"])
        self.assertEqual(warnings, [])

    def test_russell_2000_falls_back_to_vanguard_when_ishares_blocked(self):
        from tradingagents.screening import builtin_refresh

        with patch(
            "tradingagents.screening.builtin_refresh._fetch_etf_holdings_symbols",
            return_value=([], ["holdings fetch returned HTML instead of CSV"]),
        ), patch(
            "tradingagents.screening.builtin_refresh._fetch_vanguard_etf_holdings_symbols",
            return_value=(["AAA", "BBB", "CCC"], []),
        ):
            symbols, warnings = builtin_refresh._fetch_index_symbols("Russell 2000")
        self.assertEqual(len(symbols), 3)
        self.assertTrue(any("Vanguard VTWO holdings fallback" in w for w in warnings))
        self.assertFalse(any("predicate selection failed" in w for w in warnings))


if __name__ == "__main__":
    unittest.main()
