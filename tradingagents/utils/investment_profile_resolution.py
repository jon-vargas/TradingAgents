"""Source-aware investment-profile resolution for analysis entry points."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Dict, Optional, Tuple

from tradingagents.default_config import DEFAULT_CONFIG

logger = logging.getLogger("tradingagents.investment_profile_resolution")

DEFAULT_METADATA_MAX_AGE_DAYS = 7


def _profile_key_for_dict(profile: Dict[str, Any], profiles: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Recover a configured key when a caller supplied a profile dict."""
    key = str(profile.get("profile_key") or "").strip()
    if key in profiles:
        return key
    display_name = str(profile.get("display_name") or "").strip()
    for candidate_key, candidate in profiles.items():
        if profile is candidate or (
            display_name and display_name == str(candidate.get("display_name") or "").strip()
        ):
            return candidate_key
    return None


def _with_provenance(
    key: Optional[str],
    source: str,
    profiles: Dict[str, Dict[str, Any]],
    explicit_profile: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """Return a copied profile with stable key/source metadata."""
    if key and key in profiles:
        return key, {**profiles[key], "profile_key": key, "resolved_from": source}, source
    if explicit_profile:
        profile = dict(explicit_profile)
        profile.setdefault("resolved_from", source)
        return key, profile, source
    return None, None, "none"


def _is_fresh(metadata: Dict[str, Any], max_age_days: int) -> bool:
    timestamp = metadata.get("last_updated")
    if not timestamp:
        return False
    try:
        value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value >= datetime.now(timezone.utc) - timedelta(days=max(0, int(max_age_days)))


def _save_resolved_metadata(db: Any, metadata: Dict[str, Any]) -> None:
    """Persist refreshed classification via the existing metadata contract."""
    if not db or not metadata:
        return
    db.save_ticker_metadata(
        ticker=metadata.get("ticker"),
        sector=metadata.get("sector"),
        industry=metadata.get("industry"),
        market_cap=metadata.get("market_cap"),
        market_cap_tier=metadata.get("market_cap_tier"),
        is_profitable=metadata.get("is_profitable"),
        has_dividend=metadata.get("has_dividend"),
        beta=metadata.get("beta"),
        beta_tier=metadata.get("beta_tier"),
        resolved_profile=metadata.get("resolved_profile"),
        resolved_preset=metadata.get("resolved_preset"),
        asset_class=metadata.get("asset_class"),
        country=metadata.get("country"),
    )


def _reclassify_cached_metadata(metadata: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Apply current deterministic rules to fresh cached metadata."""
    if not metadata.get("sector") or not metadata.get("market_cap_tier"):
        return None
    try:
        from tradingagents.screening.ticker_resolver import resolve_profile_and_preset

        profile_key, preset_key = resolve_profile_and_preset(
            str(metadata.get("sector") or ""),
            str(metadata.get("market_cap_tier") or "unknown"),
            bool(metadata.get("is_profitable")),
            bool(metadata.get("has_dividend")),
            str(metadata.get("beta_tier") or "unknown"),
            asset_class=str(metadata.get("asset_class") or "equity"),
            industry=str(metadata.get("industry") or ""),
        )
        return profile_key, preset_key
    except Exception:
        return None


def resolve_investment_profile_for_ticker(
    ticker: str,
    *,
    explicit_key: Optional[str] = None,
    explicit_profile: Optional[Dict[str, Any]] = None,
    watchlist_id: Optional[int] = None,
    db: Any = None,
    db_path: str = "research.db",
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], str]:
    """Resolve profile in priority order without making resolution fatal.

    Priority is explicit key/profile, watchlist default, fresh metadata cache,
    metadata refresh, then generic fallback. Returns ``(key, profile, source)``.
    """
    cfg = config or DEFAULT_CONFIG
    profiles = cfg.get("investment_profiles", {})
    resolution_cfg = cfg.get("investment_profile_resolution", {})
    max_age_days = int(resolution_cfg.get("metadata_max_age_days", DEFAULT_METADATA_MAX_AGE_DAYS))

    key = str(explicit_key or "").strip()
    if key:
        return _with_provenance(key, "explicit", profiles, explicit_profile)
    if explicit_profile:
        inferred_key = _profile_key_for_dict(explicit_profile, profiles)
        return _with_provenance(inferred_key, "explicit", profiles, explicit_profile)

    if db is None:
        try:
            from tradingagents.reporting import ResearchDatabase

            db = ResearchDatabase(db_path)
        except Exception as exc:
            logger.debug("Profile resolution could not open database: %s", exc)

    if watchlist_id and db:
        try:
            watchlist = db.get_watchlist(watchlist_id) or {}
            watchlist_key = str(watchlist.get("default_investment_profile") or "").strip()
            if watchlist_key:
                return _with_provenance(watchlist_key, "watchlist", profiles)
        except Exception as exc:
            logger.debug("Profile resolution watchlist lookup failed for %s: %s", ticker, exc)

    symbol = str(ticker or "").upper().strip()
    if not symbol:
        return None, None, "none"

    if db:
        try:
            metadata = db.get_ticker_metadata(symbol) or {}
            cached_key = str(metadata.get("resolved_profile") or "").strip()
            if cached_key and _is_fresh(metadata, max_age_days):
                recomputed = _reclassify_cached_metadata(metadata)
                if recomputed and (
                    recomputed[0] != cached_key
                    or recomputed[1] != str(metadata.get("resolved_preset") or "")
                ):
                    recomputed_key, recomputed_preset = recomputed
                    metadata = {
                        **metadata,
                        "ticker": symbol,
                        "resolved_profile": recomputed_key,
                        "resolved_preset": recomputed_preset,
                    }
                    _save_resolved_metadata(db, metadata)
                    cached_key = recomputed_key
                return _with_provenance(cached_key, "metadata_cache", profiles)
        except Exception as exc:
            logger.debug("Profile resolution metadata cache lookup failed for %s: %s", symbol, exc)

    try:
        from tradingagents.screening.ticker_resolver import resolve_ticker_metadata

        refreshed = resolve_ticker_metadata(symbol) or {}
        refreshed_key = str(refreshed.get("resolved_profile") or "").strip()
        if refreshed_key:
            try:
                _save_resolved_metadata(db, refreshed)
            except Exception as exc:
                logger.debug("Profile resolution metadata save failed for %s: %s", symbol, exc)
            return _with_provenance(refreshed_key, "metadata_refresh", profiles)
    except Exception as exc:
        logger.debug("Profile metadata refresh failed for %s: %s", symbol, exc)

    return None, None, "none"
