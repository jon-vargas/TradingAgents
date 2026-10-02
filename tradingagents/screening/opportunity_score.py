"""Canonical opportunity score computation for screening results.

Single source of truth for CLI, webapp, and Scan All consolidated ranking.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def compute_opportunity_score(
    composite_score: Optional[float],
    entry_quality: Optional[float],
    macro_fit: Optional[float],
    composite_fundamental: Optional[float] = None,
    *,
    weekly_alignment: Optional[float] = None,
    weekly_alignment_dampen_threshold: float = 0.35,
    weekly_alignment_max_penalty: float = 0.08,
    use_fundamental_composite: bool = True,
    ma_crossover: Optional[float] = None,
    ma_crossover_dampen_threshold: float = 0.05,
    ma_crossover_max_penalty: float = 0.12,
) -> Tuple[float, dict]:
    """Compute blended opportunity score (0-100) with graduated penalties/bonus.

    Returns (score, metadata) where metadata includes penalty/bonus flags for
    criteria JSON logging.

    When ``composite_fundamental`` is supplied it replaces ``composite_score`` as
    the blending input to avoid double-counting EQ-overlap technicals, unless
    ``use_fundamental_composite`` is False (hunter books keep the tape Score).

    ``weekly_alignment`` (0-1) applies an alignment-only dampener when daily
    setup is strong but weekly trend is weak — no additional macro penalty.

    ``ma_crossover`` (0-1) dampens hunter-style ranks when the tape has not
    confirmed (MA≈0). Overlay rank is not a buy ticket.
    """
    meta: dict = {
        "opp_macro_penalty_applied": False,
        "weekly_alignment_dampener_applied": False,
        "ma_crossover_dampener_applied": False,
        "used_fundamental_composite": False,
    }

    if use_fundamental_composite and composite_fundamental is not None:
        composite_for_blend = composite_fundamental
        meta["used_fundamental_composite"] = True
    else:
        composite_for_blend = composite_score

    base_composite_w = 0.55
    base_eq_w = 0.30
    base_macro_w = 0.15
    if macro_fit is not None:
        regime_conviction = min(1.0, abs(float(macro_fit) - 50.0) / 50.0)
        macro_weight = base_macro_w + 0.15 * regime_conviction
        extra = macro_weight - base_macro_w
        composite_w = base_composite_w - extra * (base_composite_w / (base_composite_w + base_eq_w))
        eq_w = base_eq_w - extra * (base_eq_w / (base_composite_w + base_eq_w))
    else:
        composite_w = base_composite_w
        eq_w = base_eq_w
        macro_weight = base_macro_w

    weighted_dims: List[tuple[float, float]] = []
    if composite_for_blend is not None:
        weighted_dims.append((composite_w, float(composite_for_blend)))
    if entry_quality is not None:
        weighted_dims.append((eq_w, float(entry_quality)))
    if macro_fit is not None:
        weighted_dims.append((macro_weight, float(macro_fit)))

    if not weighted_dims:
        return 0.0, meta

    total_weight = sum(w for w, _ in weighted_dims)
    base = sum(w * v for w, v in weighted_dims) / total_weight

    if entry_quality is not None:
        eq = float(entry_quality)
        if eq < 40:
            penalty_factor = 0.70 + 0.30 * (max(0.0, eq) / 40.0)
            base *= penalty_factor

    if macro_fit is not None:
        mf = float(macro_fit)
        if mf < 45:
            meta["opp_macro_penalty_applied"] = True
            penalty_factor = 0.80 + 0.20 * (max(0.0, mf) / 45.0)
            base *= penalty_factor

    if (
        composite_for_blend is not None
        and entry_quality is not None
        and macro_fit is not None
    ):
        min_dim = min(float(composite_for_blend), float(entry_quality), float(macro_fit))
        if min_dim >= 50:
            bonus_progress = min(1.0, max(0.0, (min_dim - 50) / 15.0))
            base *= 1.0 + 0.10 * bonus_progress

    if weekly_alignment is not None:
        wa = float(weekly_alignment)
        if wa < weekly_alignment_dampen_threshold:
            # Graduated dampener: max penalty at wa=0, none at threshold
            progress = 1.0 - (wa / weekly_alignment_dampen_threshold)
            dampen = 1.0 - weekly_alignment_max_penalty * min(1.0, max(0.0, progress))
            base *= dampen
            meta["weekly_alignment_dampener_applied"] = True

    if ma_crossover is not None:
        ma = float(ma_crossover)
        if ma < ma_crossover_dampen_threshold:
            progress = 1.0 - (ma / ma_crossover_dampen_threshold) if ma_crossover_dampen_threshold else 1.0
            dampen = 1.0 - ma_crossover_max_penalty * min(1.0, max(0.0, progress))
            base *= dampen
            meta["ma_crossover_dampener_applied"] = True

    score = round(max(0.0, min(100.0, base)), 1)
    return score, meta
