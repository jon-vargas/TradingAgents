import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.reporting import ResearchDatabase, get_db

logger = logging.getLogger("tradingagents.backtesting.calibration")


def _bin_confidence(confidence: float, bins: int) -> int:
    if bins <= 0:
        return 0
    normalized = max(0.0, min(100.0, confidence))
    bin_size = 100.0 / bins
    index = int(normalized // bin_size)
    return min(index, bins - 1)


def compute_calibration(
    bins: Optional[int] = None,
    min_samples: Optional[int] = None,
    sample_limit: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, List[Tuple[float, float, int]]]:
    """Compute calibration pairs (avg_confidence, accuracy, count) per bin."""
    config_bins = bins
    config_min_samples = min_samples
    config_sample_limit = sample_limit
    try:
        from tradingagents.dataflows.config import get_config
        config = get_config()
        calibration_cfg = config.get("confidence_calibration", {})
        if config_bins is None:
            config_bins = calibration_cfg.get("bins", 5)
        if config_min_samples is None:
            config_min_samples = calibration_cfg.get("min_samples", 10)
        if config_sample_limit is None:
            config_sample_limit = calibration_cfg.get("sample_limit", 2000)
    except Exception as e:
        logger.debug("Config loading for calibration failed, using defaults: %s", e)
        if config_bins is None:
            config_bins = 5
        if config_min_samples is None:
            config_min_samples = 10
        if config_sample_limit is None:
            config_sample_limit = 2000

    bins = max(1, config_bins)  # Guard against bins=0 → KeyError when binning
    min_samples = config_min_samples
    sample_limit = config_sample_limit
    if sample_limit is not None and sample_limit <= 0:
        sample_limit = None
    db = get_db(db_path) if db_path else get_db()
    analyses = db.get_backtested_analyses(limit=sample_limit)

    bin_values: Dict[int, List[Tuple[float, float]]] = {idx: [] for idx in range(bins)}

    for analysis in analyses:
        if analysis.was_correct is None:
            continue
        bin_idx = _bin_confidence(analysis.confidence, bins)
        bin_values[bin_idx].append((analysis.confidence, 1.0 if analysis.was_correct else 0.0))

    calibration: List[Tuple[float, float, int]] = []
    total_samples = 0
    valid_bins = 0
    for idx in range(bins):
        values = bin_values[idx]
        total_samples += len(values)
        if len(values) < min_samples:
            calibration.append((-1.0, -1.0, len(values)))
            continue
        avg_conf = sum(val[0] for val in values) / len(values)
        accuracy = sum(val[1] for val in values) / len(values)
        calibration.append((round(avg_conf, 2), round(accuracy, 3), len(values)))
        valid_bins += 1

    return {
        "bins": calibration,
        "meta": {
            "bins": bins,
            "min_samples": min_samples,
            "sample_limit": sample_limit,
            "total_samples": total_samples,
            "valid_bins": valid_bins,
        },
    }


def compute_calibration_by_factor(
    bins: Optional[int] = None,
    min_samples: Optional[int] = None,
    sample_limit: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Slice calibration curves by dominant factor family from factor_scorecard.

    For each screening result that has a ``factor_scorecard`` blob in the DB
    (stored in the JSON signal fields), this function identifies the factor with
    the highest score ("dominant factor") and groups confidence-accuracy pairs
    by that factor.

    Returns:
        Dict keyed by factor family name, each containing the same structure as
        :func:`compute_calibration` plus an extra ``"dominant_factor"`` field.
        Also includes a ``"_all"`` key with the global (non-sliced) calibration.
    """
    try:
        from tradingagents.dataflows.config import get_config
        config = get_config()
        cal_cfg = config.get("confidence_calibration", {})
        bins = bins if bins is not None else cal_cfg.get("bins", 5)
        min_samples = min_samples if min_samples is not None else cal_cfg.get("min_samples", 10)
        sample_limit = sample_limit if sample_limit is not None else cal_cfg.get("sample_limit", 2000)
    except Exception:
        bins = bins or 5
        min_samples = min_samples or 10
        sample_limit = sample_limit or 2000

    n_bins = max(1, int(bins))
    db = get_db(db_path) if db_path else get_db()
    analyses = db.get_backtested_analyses(limit=sample_limit)

    # Index factor scorecards for runs referenced by backtested analyses (not just
    # the last N screening runs, which hides March scorecards when Scan All flooded).
    run_ids = sorted({
        int(getattr(a, "screening_run_id", 0) or 0)
        for a in analyses
        if getattr(a, "screening_run_id", None)
    })
    scorecard_index: Dict[str, Dict[str, float]] = {}
    for run_id in run_ids:
        try:
            results = db.get_screening_results(run_id)
        except Exception:
            continue
        for r in results:
            sigs_raw = r.get("signals", "{}")
            sigs = json.loads(sigs_raw) if isinstance(sigs_raw, str) else (sigs_raw or {})
            sc_raw = sigs.get("_factor_scorecard")
            if not sc_raw:
                continue
            sc = json.loads(sc_raw) if isinstance(sc_raw, str) else sc_raw
            if isinstance(sc, dict):
                key = f"{r.get('ticker', '')}:{run_id}"
                scorecard_index[key] = sc

    # Group backtested analyses by dominant factor
    factor_bins: Dict[str, Dict[int, List[Tuple[float, float]]]] = {}

    for analysis in analyses:
        if analysis.was_correct is None:
            continue
        ticker = getattr(analysis, "ticker", None)
        run_id = getattr(analysis, "screening_run_id", None)  # Analysis uses screening_run_id
        sc = scorecard_index.get(f"{ticker}:{run_id}") if ticker and run_id else None

        if sc:
            numeric = {
                k: float(v)
                for k, v in sc.items()
                if v is not None and k not in {"_factor_scorecard"}
            }
            dominant = max(numeric, key=numeric.get) if numeric else "_unclassified"
        else:
            dominant = "_unclassified"

        if dominant not in factor_bins:
            factor_bins[dominant] = {idx: [] for idx in range(n_bins)}
        bin_idx = _bin_confidence(analysis.confidence, n_bins)
        factor_bins[dominant][bin_idx].append((analysis.confidence, 1.0 if analysis.was_correct else 0.0))

    results_by_factor: Dict[str, Any] = {}
    for factor, bin_data in factor_bins.items():
        calibration_list: List[Tuple[float, float, int]] = []
        valid_bins_count = 0
        total = 0
        for idx in range(n_bins):
            vals = bin_data[idx]
            total += len(vals)
            if len(vals) < min_samples:
                calibration_list.append((-1.0, -1.0, len(vals)))
                continue
            avg_conf = sum(v[0] for v in vals) / len(vals)
            accuracy = sum(v[1] for v in vals) / len(vals)
            calibration_list.append((round(avg_conf, 2), round(accuracy, 3), len(vals)))
            valid_bins_count += 1
        results_by_factor[factor] = {
            "bins": calibration_list,
            "dominant_factor": factor,
            "meta": {
                "bins": n_bins,
                "min_samples": min_samples,
                "total_samples": total,
                "valid_bins": valid_bins_count,
            },
        }

    # Attach global baseline (exclude internal buckets from UI family maps)
    results_by_factor["_all"] = compute_calibration(
        bins=n_bins, min_samples=min_samples,
        sample_limit=sample_limit, db_path=db_path,
    )
    return results_by_factor


def preset_performance_families(calibration: Dict[str, Any]) -> Dict[str, Any]:
    """Return only operator-visible factor families with sample data."""
    skip = {"_all", "_unclassified"}
    return {
        k: v for k, v in (calibration or {}).items()
        if k not in skip and (v.get("meta") or {}).get("total_samples", 0) > 0
    }


def compute_calibration_by_decision(
    bins: Optional[int] = None,
    min_samples: Optional[int] = None,
    sample_limit: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Slice confidence-accuracy calibration curves by model decision (BUY / HOLD / SELL).

    Groups backtested analyses by the ``decision`` field, then computes the
    standard confidence-vs-accuracy calibration curve for each group.  This
    exposes systematic model biases per decision class (e.g. BUY-side
    overconfidence).

    Returns:
        Dict keyed by normalized decision string (e.g. ``"BUY"``, ``"HOLD"``,
        ``"SELL"``).  Each value has the same shape as :func:`compute_calibration`
        plus a ``"decision"`` field.  Also includes an ``"_all"`` global key.
    """
    try:
        from tradingagents.dataflows.config import get_config
        config = get_config()
        cal_cfg = config.get("confidence_calibration", {})
        bins = bins if bins is not None else cal_cfg.get("bins", 5)
        min_samples = min_samples if min_samples is not None else cal_cfg.get("min_samples", 10)
        sample_limit = sample_limit if sample_limit is not None else cal_cfg.get("sample_limit", 2000)
    except Exception:
        bins = bins or 5
        min_samples = min_samples or 10
        sample_limit = sample_limit or 2000

    n_bins = max(1, int(bins))
    db = get_db(db_path) if db_path else get_db()
    analyses = db.get_backtested_analyses(limit=sample_limit)

    # Normalize decision strings to BUY / HOLD / SELL
    _DECISION_MAP = {
        "buy": "BUY", "strong buy": "BUY", "bullish": "BUY",
        "sell": "SELL", "strong sell": "SELL", "bearish": "SELL",
        "hold": "HOLD", "neutral": "HOLD", "watch": "HOLD",
    }

    decision_bins: Dict[str, Dict[int, List[Tuple[float, float]]]] = {}

    for analysis in analyses:
        if analysis.was_correct is None:
            continue
        raw_decision = (getattr(analysis, "decision", "") or "").strip().lower()
        # Try normalizing multi-word decisions (e.g. "BUY - confident")
        normalized = None
        for key, val in _DECISION_MAP.items():
            if raw_decision.startswith(key):
                normalized = val
                break
        normalized = normalized or "OTHER"

        if normalized not in decision_bins:
            decision_bins[normalized] = {idx: [] for idx in range(n_bins)}
        bin_idx = _bin_confidence(analysis.confidence, n_bins)
        decision_bins[normalized][bin_idx].append(
            (analysis.confidence, 1.0 if analysis.was_correct else 0.0)
        )

    results_by_decision: Dict[str, Any] = {}
    for decision, bin_data in decision_bins.items():
        calibration_list: List[Tuple[float, float, int]] = []
        valid_bins_count = 0
        total = 0
        for idx in range(n_bins):
            vals = bin_data[idx]
            total += len(vals)
            if len(vals) < min_samples:
                calibration_list.append((-1.0, -1.0, len(vals)))
                continue
            avg_conf = sum(v[0] for v in vals) / len(vals)
            accuracy = sum(v[1] for v in vals) / len(vals)
            calibration_list.append((round(avg_conf, 2), round(accuracy, 3), len(vals)))
            valid_bins_count += 1
        results_by_decision[decision] = {
            "bins": calibration_list,
            "decision": decision,
            "meta": {
                "bins": n_bins,
                "min_samples": min_samples,
                "total_samples": total,
                "valid_bins": valid_bins_count,
            },
        }

    # Attach global baseline
    results_by_decision["_all"] = compute_calibration(
        bins=n_bins, min_samples=min_samples,
        sample_limit=sample_limit, db_path=db_path,
    )
    return results_by_decision


def calibrate_score(raw_confidence: float, calibration: Dict[str, List[Tuple[float, float, int]]]) -> float:
    """Adjust confidence based on calibration accuracy curve."""
    bins = calibration.get("bins", [])
    if not bins:
        return raw_confidence

    # Find nearest bin by avg confidence
    target = raw_confidence
    eligible = [item for item in bins if item[0] >= 0 and item[1] >= 0]
    if not eligible:
        return raw_confidence
    closest = min(eligible, key=lambda item: abs(item[0] - target))
    _, accuracy, count = closest
    # Blend raw confidence with empirical accuracy
    adjusted = (raw_confidence * 0.5) + (accuracy * 100.0 * 0.5)
    return round(adjusted, 2)
