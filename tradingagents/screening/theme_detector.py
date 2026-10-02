"""
ThemeDetector — Rule-based mapping from market conditions to themed
discovery prompts for auto-generated watchlists.

Inputs (v1):
  - Top sectors by 20d momentum (from compute_sector_momentum())
  - Market regime (from get_macro_snapshot())
  - VIX level
  - IWM vs SPY relative return

Each theme template specifies activation conditions, a Perplexity prompt,
screening preset, investment profile, and a priority weight so the system
can rank and select the top N themes per run.
"""

import logging
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

logger = logging.getLogger("tradingagents.screening.theme_detector")


@dataclass
class ThemeSpec:
    """A single themed discovery specification ready for Perplexity."""
    name: str
    theme: str
    criteria: str
    market_cap_filter: str
    preset: str
    profile: str
    rationale: str
    primary_sector: str
    priority: float = 0.0
    catalog_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# Maps auto-discovery activation templates onto the approved Discover hopper
# catalog so Perplexity calls inherit listing / fit / sector gates.
THEME_CATALOG_IDS: Dict[str, str] = {
    "ai_infra_bull": "ai_infrastructure",
    "semi_ai_capex": "semiconductor_ai",
    "biotech_catalyst": "biotech_catalysts",
    "medtech_growth": "biotech_catalysts",
    "energy_insider": "energy_producers",
    "clean_energy": "energy_producers",
    "defense_space": "defense_space",
    "fintech_bull": "mid_cap_momentum",
    "consumer_ecommerce": "small_cap_momentum",
    "speculative_micro": "micro_momentum",
    "short_squeeze_setups": "squeeze_setups",
    "streaming_media": "mid_cap_momentum",
    "defensive_dividend": "dividend_growers",
    "counter_cyclical": "quality_compounders",
    "materials_supercycle": "critical_minerals",
    "data_center_reits": "ai_infrastructure",
    "grid_modernization": "dividend_growers",
}


# =========================================================================
# Theme Template Registry
# =========================================================================

@dataclass
class _ThemeTemplate:
    """Internal template — activation conditions + prompt content."""
    id: str
    watchlist_name: str
    sectors: List[str]
    regimes: List[str]
    vix_max: Optional[float]
    vix_min: Optional[float]
    iwm_outperform: Optional[bool]
    theme: str
    criteria: str
    market_cap_filter: str
    preset: str
    profile: str
    base_priority: float


_THEME_TEMPLATES: List[_ThemeTemplate] = [
    # ---- Technology / Bull ----
    _ThemeTemplate(
        id="ai_infra_bull",
        watchlist_name="Auto: AI Infrastructure",
        sectors=["Technology"],
        regimes=["bull"],
        vix_max=25.0, vix_min=None, iwm_outperform=None,
        theme="Small-cap AI and cloud infrastructure companies with accelerating revenue",
        criteria="Prefer companies with >20% YoY revenue growth and recent enterprise customer wins",
        market_cap_filter="under $5B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=90,
    ),
    _ThemeTemplate(
        id="semi_ai_capex",
        watchlist_name="Auto: Semiconductor AI Capex",
        sectors=["Technology"],
        regimes=["bull", "neutral"],
        vix_max=30.0, vix_min=None, iwm_outperform=None,
        theme="Semiconductor companies benefiting from the AI capex cycle",
        criteria="Focus on fabless designers, packaging/testing, and EDA tooling",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=85,
    ),
    # ---- Healthcare ----
    _ThemeTemplate(
        id="biotech_catalyst",
        watchlist_name="Auto: Biotech Pre-Catalyst",
        sectors=["Healthcare"],
        regimes=["bull", "neutral"],
        vix_max=30.0, vix_min=None, iwm_outperform=None,
        theme="Biotech companies with upcoming FDA catalysts in the next 6 months",
        criteria="Phase 2/3 readouts, PDUFA dates, or NDA/BLA submissions; prefer orphan drug or first-in-class",
        market_cap_filter="under $5B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=80,
    ),
    _ThemeTemplate(
        id="medtech_growth",
        watchlist_name="Auto: MedTech Growth",
        sectors=["Healthcare"],
        regimes=["bull"],
        vix_max=20.0, vix_min=None, iwm_outperform=None,
        theme="Medical device and health-tech companies with rapid adoption curves",
        criteria="Surgical robotics, remote patient monitoring, AI diagnostics",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=70,
    ),
    # ---- Energy ----
    _ThemeTemplate(
        id="energy_insider",
        watchlist_name="Auto: Energy Insider Buying",
        sectors=["Energy"],
        regimes=["bull", "neutral"],
        vix_max=None, vix_min=None, iwm_outperform=True,
        theme="Small-cap energy producers with rising insider buying and growing production",
        criteria="Permian Basin, natural gas, or LNG export exposure preferred",
        market_cap_filter="under $5B",
        preset="commodity_cyclical",
        profile="commodity_cyclical",
        base_priority=75,
    ),
    _ThemeTemplate(
        id="clean_energy",
        watchlist_name="Auto: Clean Energy Transition",
        sectors=["Energy", "Industrials"],
        regimes=["bull"],
        vix_max=22.0, vix_min=None, iwm_outperform=None,
        theme="Clean energy and grid infrastructure companies with government contract catalysts",
        criteria="Solar, wind, grid storage, or EV charging; IRA beneficiaries preferred",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=72,
    ),
    # ---- Industrials ----
    _ThemeTemplate(
        id="defense_space",
        watchlist_name="Auto: Defense & Space Tech",
        sectors=["Industrials"],
        regimes=["bull", "neutral", "bear"],
        vix_max=None, vix_min=None, iwm_outperform=None,
        theme="Space technology and defense companies with recent government contracts",
        criteria="Focus on satellite, launch, hypersonics, and autonomous systems",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=78,
    ),
    # ---- Financial Services ----
    _ThemeTemplate(
        id="fintech_bull",
        watchlist_name="Auto: Fintech Disruptors",
        sectors=["Financial Services"],
        regimes=["bull"],
        vix_max=22.0, vix_min=None, iwm_outperform=None,
        theme="Fintech companies disrupting payments, lending, or capital markets",
        criteria="Prefer profitable or near-profitable with >30% revenue growth",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=68,
    ),
    # ---- Consumer Cyclical ----
    _ThemeTemplate(
        id="consumer_ecommerce",
        watchlist_name="Auto: E-Commerce Growth",
        sectors=["Consumer Cyclical"],
        regimes=["bull"],
        vix_max=20.0, vix_min=None, iwm_outperform=True,
        theme="Small-cap e-commerce and direct-to-consumer brands with expanding margins",
        criteria="Focus on proprietary brands, not marketplaces; improving unit economics",
        market_cap_filter="under $5B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=65,
    ),
    # ---- Speculative micro-cap (cross-sector, risk-on) ----
    _ThemeTemplate(
        id="speculative_micro",
        watchlist_name="Auto: Speculative Micro-Cap",
        sectors=[],  # Any sector -- activated by VIX + IWM conditions
        regimes=["bull"],
        vix_max=15.0, vix_min=None, iwm_outperform=True,
        theme="Speculative micro-cap technology with recent institutional interest",
        criteria="Recent 13F filings showing new institutional positions; under-followed by analysts",
        market_cap_filter="under $500M",
        preset="momentum_hunter",
        profile="momentum_speculative",
        base_priority=60,
    ),
    _ThemeTemplate(
        id="short_squeeze_setups",
        watchlist_name="Auto: High Short-Interest Setups",
        sectors=[],  # Cross-sector, regime-conditional
        regimes=["bull", "neutral"],
        vix_max=32.0, vix_min=None, iwm_outperform=True,
        theme="High short-interest small and mid caps with improving momentum and catalyst flow",
        criteria="Prefer names with elevated short float, rising volume, and fresh catalysts in the last 6 months",
        market_cap_filter="under $10B",
        preset="short_squeeze",
        profile="momentum_speculative",
        base_priority=77,
    ),
    # ---- Communication Services ----
    _ThemeTemplate(
        id="streaming_media",
        watchlist_name="Auto: Digital Media & Streaming",
        sectors=["Communication Services"],
        regimes=["bull", "neutral"],
        vix_max=25.0, vix_min=None, iwm_outperform=None,
        theme="Digital media, streaming, and gaming companies with subscriber growth",
        criteria="Prefer ad-tech, CTV, or interactive entertainment with improving ARPU",
        market_cap_filter="under $10B",
        preset="momentum_hunter",
        profile="high_growth",
        base_priority=62,
    ),
    # ---- Defensive / Bear ----
    _ThemeTemplate(
        id="defensive_dividend",
        watchlist_name="Auto: Defensive Dividend Growers",
        sectors=[],  # Any sector -- activated by bear regime
        regimes=["bear"],
        vix_max=None, vix_min=20.0, iwm_outperform=None,
        theme="Defensive dividend growers with strong free cash flow and pricing power",
        criteria="Consumer staples, utilities, or healthcare with >10 years of dividend growth",
        market_cap_filter="over $5B",
        preset="dividend_income",
        profile="dividend_income",
        base_priority=85,
    ),
    _ThemeTemplate(
        id="counter_cyclical",
        watchlist_name="Auto: Counter-Cyclical Value",
        sectors=[],  # Any sector
        regimes=["bear"],
        vix_max=None, vix_min=None, iwm_outperform=None,
        theme="Counter-cyclical companies with pricing power trading below intrinsic value",
        criteria="Waste management, insurance, discount retail, or essential services",
        market_cap_filter="any",
        preset="quality_compounder",
        profile="large_cap_core",
        base_priority=80,
    ),
    # ---- Basic Materials ----
    _ThemeTemplate(
        id="materials_supercycle",
        watchlist_name="Auto: Critical Minerals",
        sectors=["Basic Materials"],
        regimes=["bull", "neutral"],
        vix_max=None, vix_min=None, iwm_outperform=None,
        theme="Critical mineral miners and processors for EV batteries and defense supply chains",
        criteria="Lithium, rare earths, copper, or cobalt with expanding production capacity",
        market_cap_filter="under $10B",
        preset="commodity_cyclical",
        profile="commodity_cyclical",
        base_priority=70,
    ),
    # ---- Real Estate ----
    _ThemeTemplate(
        id="data_center_reits",
        watchlist_name="Auto: Data Center REITs",
        sectors=["Real Estate", "Technology"],
        regimes=["bull", "neutral"],
        vix_max=25.0, vix_min=None, iwm_outperform=None,
        theme="Data center and digital infrastructure REITs benefiting from AI compute demand",
        criteria="Prefer hyperscale colocation with long-term leases and expanding capacity",
        market_cap_filter="any",
        preset="quality_compounder",
        profile="dividend_income",
        base_priority=73,
    ),
    # ---- Utilities (growth angle) ----
    _ThemeTemplate(
        id="grid_modernization",
        watchlist_name="Auto: Grid Modernization",
        sectors=["Utilities"],
        regimes=["bull", "neutral"],
        vix_max=None, vix_min=None, iwm_outperform=None,
        theme="Utility and grid infrastructure companies investing in AI-driven power demand",
        criteria="Nuclear, smart grid, or transmission buildout with rate base growth >5%",
        market_cap_filter="any",
        preset="dividend_income",
        profile="dividend_income",
        base_priority=66,
    ),
]


# =========================================================================
# ThemeDetector
# =========================================================================

class ThemeDetector:
    """Detect trending themes from market data and produce discovery specs."""

    def __init__(self, max_themes: int = 5):
        self.max_themes = max_themes

    def detect_themes(
        self,
        sector_momentum: Dict[str, Any],
        macro_snapshot: Dict[str, Any],
    ) -> List[ThemeSpec]:
        """Evaluate all theme templates against current market conditions.

        Returns up to ``max_themes`` :class:`ThemeSpec` objects, ranked by
        adjusted priority.
        """
        regime = macro_snapshot.get("market_regime", "unknown")
        regime_label = macro_snapshot.get("index_regime_label")
        if not regime_label or regime_label == "Unknown":
            from tradingagents.dataflows.index_regime import index_regime_display_label
            regime_label = index_regime_display_label(macro_snapshot)
        vix_value = macro_snapshot.get("vix_value")
        if vix_value is None:
            vix_data = macro_snapshot.get("indices", {}).get("vix", {})
            vix_value = vix_data.get("current", 20.0) if isinstance(vix_data, dict) else 20.0

        sector_ranks: Dict[str, int] = sector_momentum.get("sector_ranks", {})
        iwm_vs_spy = sector_momentum.get("iwm_vs_spy_20d", 0.0)
        iwm_outperforming = iwm_vs_spy > 0

        top_3_sectors = {s for s, r in sector_ranks.items() if r <= 3}

        candidates: List[ThemeSpec] = []

        for tmpl in _THEME_TEMPLATES:
            if not self._matches(tmpl, regime, vix_value, iwm_outperforming, top_3_sectors, sector_ranks):
                continue

            primary = self._pick_primary_sector(tmpl, top_3_sectors, sector_ranks)
            priority = self._compute_priority(tmpl, sector_ranks, primary, iwm_vs_spy)

            rationale_parts = []
            if primary and primary in sector_ranks:
                rationale_parts.append(f"{primary} ranked #{sector_ranks[primary]} by 20d sector momentum")
            if regime != "unknown":
                rationale_parts.append(f"{regime_label} index regime")
            rationale_parts.append(f"VIX={vix_value:.1f}")
            if tmpl.iwm_outperform is not None:
                rationale_parts.append(f"IWM vs SPY 20d={iwm_vs_spy:+.2f}%")

            catalog_id = THEME_CATALOG_IDS.get(tmpl.id, "")
            theme = tmpl.theme
            criteria = tmpl.criteria
            market_cap_filter = tmpl.market_cap_filter
            preset = tmpl.preset
            profile = tmpl.profile
            if catalog_id:
                from tradingagents.screening.discovery import get_discovery_template
                catalog = get_discovery_template(catalog_id)
                if catalog:
                    theme = catalog.theme
                    criteria = catalog.criteria
                    market_cap_filter = catalog.market_cap_filter
                    preset = catalog.preset
                    profile = catalog.profile

            candidates.append(ThemeSpec(
                name=tmpl.watchlist_name,
                theme=theme,
                criteria=criteria,
                market_cap_filter=market_cap_filter,
                preset=preset,
                profile=profile,
                rationale="; ".join(rationale_parts),
                primary_sector=primary,
                priority=priority,
                catalog_id=catalog_id,
            ))

        candidates.sort(key=lambda t: t.priority, reverse=True)
        selected = candidates[: self.max_themes]
        logger.info(
            "ThemeDetector: %d/%d templates matched, selected top %d",
            len(candidates), len(_THEME_TEMPLATES), len(selected),
        )
        for spec in selected:
            logger.debug("  -> %s (priority=%.1f)", spec.name, spec.priority)
        return selected

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _matches(
        tmpl: _ThemeTemplate,
        regime: str,
        vix: float,
        iwm_outperforming: bool,
        top_3: set,
        sector_ranks: Dict[str, int],
    ) -> bool:
        """Check whether a template's activation conditions are met."""
        if regime == "unknown":
            return False

        if tmpl.regimes and regime not in tmpl.regimes:
            return False
        if tmpl.vix_max is not None and vix > tmpl.vix_max:
            return False
        if tmpl.vix_min is not None and vix < tmpl.vix_min:
            return False
        if tmpl.iwm_outperform is True and not iwm_outperforming:
            return False
        if tmpl.iwm_outperform is False and iwm_outperforming:
            return False

        if tmpl.sectors:
            if not any(s in top_3 or sector_ranks.get(s, 12) <= 5 for s in tmpl.sectors):
                return False

        return True

    @staticmethod
    def _pick_primary_sector(
        tmpl: _ThemeTemplate,
        top_3: set,
        sector_ranks: Dict[str, int],
    ) -> str:
        """Choose the most relevant sector from the template's sector list."""
        if not tmpl.sectors:
            return "Cross-Sector"
        best_sector = tmpl.sectors[0]
        best_rank = sector_ranks.get(best_sector, 12)
        for s in tmpl.sectors[1:]:
            r = sector_ranks.get(s, 12)
            if r < best_rank:
                best_sector = s
                best_rank = r
        return best_sector

    @staticmethod
    def _compute_priority(
        tmpl: _ThemeTemplate,
        sector_ranks: Dict[str, int],
        primary_sector: str,
        iwm_vs_spy: float,
    ) -> float:
        """Adjusted priority: base priority boosted by sector rank strength."""
        priority = tmpl.base_priority
        if primary_sector and primary_sector != "Cross-Sector":
            rank = sector_ranks.get(primary_sector, 6)
            priority += max(0, (6 - rank)) * 2
        if iwm_vs_spy > 1.0:
            priority += 3
        return priority


# =========================================================================
# Drift Score Calculation
# =========================================================================

def compute_drift(stored_snapshot: dict, current_snapshot: dict) -> float:
    """Return 0.0 (no change) to 1.0 (completely different market).

    Used to decide whether an auto-watchlist's theme is still valid.
    """
    primary_sector = stored_snapshot.get("primary_sector", "")
    stored_ranks = stored_snapshot.get("sector_ranks", {})
    current_ranks = current_snapshot.get("sector_ranks", {})

    old_rank = stored_ranks.get(primary_sector, 6)
    new_rank = current_ranks.get(primary_sector, 6)
    rank_drift = abs(new_rank - old_rank) / 10.0

    old_regime = stored_snapshot.get("regime", "unknown")
    new_regime = current_snapshot.get("regime", "unknown")
    if old_regime == "unknown" or new_regime == "unknown":
        regime_drift = 0.0
    else:
        regime_map = {"bull": 0, "neutral": 1, "bear": 2}
        old_r = regime_map.get(old_regime, 1)
        new_r = regime_map.get(new_regime, 1)
        regime_drift = abs(new_r - old_r) / 2.0

    return round(rank_drift * 0.6 + regime_drift * 0.4, 3)
