import tempfile
from pathlib import Path

from tradingagents.backtesting import compute_calibration
from tradingagents.reporting import ResearchDatabase, Analysis


def _write_analysis(db: ResearchDatabase, ticker: str, confidence: float, was_correct: bool):
    analysis = Analysis(
        ticker=ticker,
        analysis_date="2026-01-01",
        decision="BUY",
        confidence=confidence,
        was_correct=was_correct,
    )
    db.save_analysis(analysis)


def test_calibration_bins():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = Path(temp_dir) / "test_research.db"
        db = ResearchDatabase(str(db_path))
        _write_analysis(db, "AAA", 25.0, True)
        _write_analysis(db, "BBB", 75.0, False)

        calibration = compute_calibration(bins=2, min_samples=1, db_path=str(db_path))
        bins = calibration["bins"]

        assert len(bins) == 2
        assert bins[0] == (25.0, 1.0, 1)
        assert bins[1] == (75.0, 0.0, 1)


def test_preset_performance_families_skips_internal_buckets():
    from tradingagents.backtesting.calibration import preset_performance_families

    payload = {
        "_all": {"meta": {"total_samples": 50}},
        "_unclassified": {"meta": {"total_samples": 40}},
        "Momentum": {"meta": {"total_samples": 12}},
        "Value": {"meta": {"total_samples": 0}},
    }
    out = preset_performance_families(payload)
    assert list(out.keys()) == ["Momentum"]
