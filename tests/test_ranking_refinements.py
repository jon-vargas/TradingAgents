"""Unit tests for the ranking-algorithm refinements shipped in the
"opportunity score v2" pass:

1.  ``_determine_direction`` weight-normalized + neutral band (no bullish bias)
2.  ``_compute_composite_fundamental_score`` excludes EQ-overlap signals
3.  ``_compute_opportunity_score`` graduated penalties / confluence bonus
4.  ``_compute_opportunity_score`` regime-weighted macro blend
5.  ``_compute_opportunity_score`` consumes ``composite_fundamental`` when
    provided and falls back to ``composite_score`` otherwise.

The intent of these tests is to lock in the new behaviour so later refactors
don't silently regress the ranking math. They do NOT attempt to validate the
ranking's financial usefulness — that's the backtest/calibration harness'
responsibility.
"""
import unittest


class _EngineStub:
    """Minimal shim so we can call bound methods without constructing the
    full ScreeningEngine (which needs DB + config). We reach into
    ScreeningEngine via __new__ + the polarity constants + the config dict."""

    def _build(self):
        from tradingagents.screening.engine import ScreeningEngine
        engine = ScreeningEngine.__new__(ScreeningEngine)
        engine._screening_config = {"direction_conviction_threshold": 0.05}
        return engine


class TestDirectionNeutralBand(unittest.TestCase, _EngineStub):
    def test_all_zero_signals_labels_neutral_not_bullish(self):
        engine = self._build()
        signals = {name: 0.0 for name in engine.SIGNAL_POLARITY}
        weights = {name: 1.0 for name in engine.SIGNAL_POLARITY}
        self.assertEqual(engine._determine_direction(signals, weights), "neutral")

    def test_small_positive_residual_stays_neutral(self):
        engine = self._build()
        # Only one bullish signal barely positive; should NOT trip into bullish.
        signals = {name: 0.0 for name in engine.SIGNAL_POLARITY}
        signals["relative_strength"] = 0.02  # tiny
        weights = {name: 1.0 for name in engine.SIGNAL_POLARITY}
        self.assertEqual(engine._determine_direction(signals, weights), "neutral")

    def test_clear_bearish_is_labeled_bearish(self):
        engine = self._build()
        signals = {name: 0.0 for name in engine.SIGNAL_POLARITY}
        signals["rsi_overbought"] = 0.9  # -1 polarity → bearish
        weights = {name: 1.0 for name in engine.SIGNAL_POLARITY}
        self.assertEqual(engine._determine_direction(signals, weights), "bearish")

    def test_clear_bullish_is_labeled_bullish(self):
        engine = self._build()
        signals = {name: 0.0 for name in engine.SIGNAL_POLARITY}
        signals["relative_strength"] = 0.8
        signals["smart_money"] = 0.8
        signals["estimate_momentum"] = 0.8
        weights = {name: 1.0 for name in engine.SIGNAL_POLARITY}
        self.assertEqual(engine._determine_direction(signals, weights), "bullish")

    def test_live_weights_overbought_still_bearish(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        engine = self._build()
        weights = dict(DEFAULT_CONFIG["screening"]["signal_weights"])
        signals = {"rsi_overbought": 0.9}
        self.assertGreater(weights["rsi_overbought"], 0.0)
        self.assertEqual(engine._determine_direction(signals, weights), "bearish")

    def test_zero_directional_weight_is_neutral(self):
        engine = self._build()
        signals = {name: 0.9 for name in engine.SIGNAL_POLARITY}
        weights = {name: 0.0 for name in engine.SIGNAL_POLARITY}
        self.assertEqual(engine._determine_direction(signals, weights), "neutral")


class TestCompositeFundamentalDisentanglement(unittest.TestCase, _EngineStub):
    def test_excludes_overlap_signals(self):
        engine = self._build()
        signals = {
            "rsi_oversold": 1.0,       # overlap → excluded
            "bollinger_squeeze": 1.0,  # overlap → excluded
            "ma_crossover": 1.0,       # overlap → excluded
            "volume_surge": 1.0,       # overlap → excluded
            "trend_strength": 1.0,     # overlap → excluded
            "valuation_gap": 0.2,      # retained (low)
            "estimate_momentum": 0.2,  # retained (low)
        }
        weights = {k: 1.0 for k in signals}
        composite_full = engine._compute_composite_score(signals, weights)
        composite_fund = engine._compute_composite_fundamental_score(signals, weights)
        # Full composite renormalizes across all seven signals (five high overlap +
        # two low fundamental) → well above the fundamental-only score.
        self.assertGreater(composite_full, composite_fund)
        self.assertAlmostEqual(composite_fund, 20.0, places=4)

    def test_pathological_all_overlap_falls_back_to_full_composite(self):
        engine = self._build()
        signals = {
            "rsi_oversold": 0.5,
            "bollinger_squeeze": 0.5,
            "ma_crossover": 0.5,
        }
        weights = {k: 1.0 for k in signals}
        full = engine._compute_composite_score(signals, weights)
        fund = engine._compute_composite_fundamental_score(signals, weights)
        self.assertAlmostEqual(full, fund)


class TestOpportunityScoreGraduation(unittest.TestCase):
    def _opp(self, **kw):
        from tradingagents.screening.opportunity_score import compute_opportunity_score

        score, _ = compute_opportunity_score(**kw)
        return score

    def test_graduated_eq_penalty_no_cliff_at_25(self):
        # Two rows straddling the old hard-cliff at eq=25 should have very
        # close opportunity scores now (graduated) rather than a 25% gap.
        low = self._opp(composite_score=60, entry_quality=24, macro_fit=50)
        high = self._opp(composite_score=60, entry_quality=26, macro_fit=50)
        self.assertLess(high - low, 2.0, "Adjacent EQ values should not straddle a cliff")

    def test_confluence_bonus_ramps_in(self):
        # At min(dim)=50 → bonus==1.0; at min(dim)>=65 → bonus==1.10.
        low = self._opp(composite_score=50, entry_quality=50, macro_fit=50)
        mid = self._opp(composite_score=58, entry_quality=58, macro_fit=58)
        high = self._opp(composite_score=65, entry_quality=65, macro_fit=65)
        self.assertLess(low, mid)
        self.assertLess(mid, high)

    def test_composite_fundamental_preferred_when_provided(self):
        # composite_fundamental=20 should drag the opp score lower than
        # composite_score=80 alone would.
        with_fund = self._opp(composite_score=80, entry_quality=50, macro_fit=50, composite_fundamental=20)
        without_fund = self._opp(composite_score=80, entry_quality=50, macro_fit=50)
        self.assertLess(with_fund, without_fund)

    def test_regime_risk_off_weight_amplifies_macro(self):
        # Same composite/EQ, but macro=20 (risk-off) vs macro=50 (neutral).
        # The regime-weighted blend should produce a *larger* drop for the
        # risk-off case than a plain 0.15 macro weight would have.
        neutral_macro = self._opp(composite_score=70, entry_quality=60, macro_fit=50)
        risk_off = self._opp(composite_score=70, entry_quality=60, macro_fit=20)
        gap = neutral_macro - risk_off
        # With static 0.15 macro weight, gap would be ~(50-20) * 0.15 = 4.5.
        # With the regime-weighted blend the effective gap should be notably
        # larger. Assert at least 5.0 to confirm amplification (accounting
        # for penalty multipliers applied to base).
        self.assertGreater(gap, 5.0)

    def test_missing_macro_does_not_crash(self):
        score = self._opp(composite_score=60, entry_quality=60, macro_fit=None)
        self.assertTrue(0 <= score <= 100)

    def test_missing_everything_returns_zero(self):
        score = self._opp(composite_score=None, entry_quality=None, macro_fit=None)
        self.assertEqual(score, 0.0)

    def test_hunter_uses_tape_not_fundamental_composite(self):
        from tradingagents.screening.opportunity_score import compute_opportunity_score

        fund, fund_meta = compute_opportunity_score(
            40.0, 70.0, 75.0, composite_fundamental=74.0, use_fundamental_composite=True,
        )
        tape, tape_meta = compute_opportunity_score(
            40.0, 70.0, 75.0, composite_fundamental=74.0, use_fundamental_composite=False,
        )
        self.assertTrue(fund_meta["used_fundamental_composite"])
        self.assertFalse(tape_meta["used_fundamental_composite"])
        self.assertLess(tape, fund)

    def test_ma_crossover_dampener_lowers_unconfirmed_tape(self):
        from tradingagents.screening.opportunity_score import compute_opportunity_score

        confirmed, confirmed_meta = compute_opportunity_score(
            55.0, 70.0, 75.0, use_fundamental_composite=False, ma_crossover=1.0,
        )
        unconfirmed, unconfirmed_meta = compute_opportunity_score(
            55.0, 70.0, 75.0, use_fundamental_composite=False, ma_crossover=0.0,
        )
        self.assertFalse(confirmed_meta["ma_crossover_dampener_applied"])
        self.assertTrue(unconfirmed_meta["ma_crossover_dampener_applied"])
        self.assertLess(unconfirmed, confirmed)


if __name__ == "__main__":
    unittest.main()
