"""
Targeted tests for built-in refresh apply-gate enforcement:
  - high-churn proposals blocked without force_reason
  - high-churn proposals pass with force_reason
  - sparse-guard warning text and retain-existing behavior
"""

import copy
import os
import tempfile
import unittest
from unittest.mock import patch


class _FakeSparseDB:
    """Minimal DB stub for sparse-guard proposal generation tests."""

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
                "target_size": 10,
                "tickers": ",".join(f"T{i}" for i in range(10)),
            }
        ]
        self._watchlists = [
            {
                "id": 1,
                "name": "Russell 2000 Top 100",
                "tickers": ",".join(f"T{i}" for i in range(10)),
            }
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


class TestBuiltinRefreshApplyGate(unittest.TestCase):
    def _make_db_with_high_churn_proposal(self, watchlist_id, db):
        """Insert a proposal that has 100% churn (all tickers replaced)."""
        proposal_id = db.create_builtin_refresh_proposal(
            items=[
                {
                    "watchlist_id": watchlist_id,
                    "watchlist_name": "Apply Gate Test",
                    "old_tickers": ["AAA", "BBB"],
                    "new_tickers": ["CCC", "DDD"],
                    "warnings": ["high churn warning (100.0% > 40.0%)"],
                }
            ],
            source="test",
            metadata={"warnings": ["Apply Gate Test: high churn warning (100.0% > 40.0%)"]},
        )
        return proposal_id

    def _make_temp_db(self):
        from tradingagents.reporting.database import ResearchDatabase

        fd, db_path = tempfile.mkstemp(prefix="apply_gate_", suffix=".db")
        os.close(fd)
        db = ResearchDatabase(db_path)
        watchlist_id = int(
            db.create_watchlist(
                name="Apply Gate Test",
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
        return db, db_path, watchlist_id

    def test_high_churn_proposal_blocked_without_force_reason(self):
        """apply_builtin_refresh_proposal returns requires_force_apply when churn exceeds threshold."""
        db, db_path, wl_id = self._make_temp_db()
        try:
            proposal_id = self._make_db_with_high_churn_proposal(wl_id, db)
            guard_cfg = {
                "enforce_high_risk_apply": True,
                "require_force_reason": True,
                "force_apply_churn_pct": 0.40,
            }
            result = db.apply_builtin_refresh_proposal(proposal_id, guard_config=guard_cfg)
            self.assertFalse(result["applied"])
            self.assertTrue(result.get("requires_force_apply"))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_high_churn_proposal_passes_with_force_reason(self):
        """apply_builtin_refresh_proposal succeeds when force_apply=True + non-empty force_reason."""
        db, db_path, wl_id = self._make_temp_db()
        try:
            proposal_id = self._make_db_with_high_churn_proposal(wl_id, db)
            guard_cfg = {
                "enforce_high_risk_apply": True,
                "require_force_reason": True,
                "force_apply_churn_pct": 0.40,
            }
            result = db.apply_builtin_refresh_proposal(
                proposal_id,
                force_apply=True,
                force_reason="Index rebalance: quarterly reconstitution",
                guard_config=guard_cfg,
            )
            self.assertTrue(result["applied"])
            self.assertTrue(result.get("forced_apply"))
        finally:
            try:
                os.remove(db_path)
            except OSError:
                pass

    def test_sparse_guard_retains_existing_list(self):
        """Russell sparse validation retains existing tickers and surfaces clear warning."""
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.screening.builtin_refresh import build_refresh_proposal

        db = _FakeSparseDB()
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        refresh_cfg = cfg["screening"]["scheduler"]["builtin_refresh"]
        refresh_cfg["allow_sparse_baseline_fallback"] = False
        refresh_cfg["sparse_validation_min_ratio"] = 0.90
        refresh_cfg["validation_workers"] = 2
        refresh_cfg["min_size_by_list"]["Russell 2000 Top 100"] = 1

        # Only 1 of 10 symbols validates — far below 90% threshold
        existing = [f"T{i}" for i in range(10)]
        with patch(
            "tradingagents.screening.builtin_refresh._validate_symbols_tiered",
            return_value=(["T0"], [f"T{i}" for i in range(1, 10)], {}, {"tier1_validated": 1, "tier2_validated": 0, "tier3_validated": 0, "tier3_attempted": 0}),
        ), patch(
            "tradingagents.screening.builtin_refresh._hydrate_market_caps",
            return_value=({"T0": 500_000_000.0}, 0),
        ):
            proposal = build_refresh_proposal(db=db, config=cfg, source="test")

        item = proposal["items"][0]
        self.assertEqual(item["watchlist_name"], "Russell 2000 Top 100")
        # Existing list should be retained
        self.assertEqual(sorted(item["new_tickers"]), sorted(existing))
        # Warning must contain "sparse validation guard" and "retained existing list"
        sparse_warns = [w for w in item["warnings"] if "sparse validation guard" in w]
        self.assertTrue(
            sparse_warns,
            msg=f"Expected sparse validation guard warning; got: {item['warnings']}",
        )
        self.assertTrue(
            any("retained existing list" in w for w in sparse_warns),
            msg=f"Warning should mention 'retained existing list'; got: {sparse_warns}",
        )


if __name__ == "__main__":
    unittest.main()
