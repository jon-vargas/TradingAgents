"""
API Usage Tracker - Monitor and manage API rate limits and credits.

Features:
- Track usage per vendor/API
- Warn when approaching limits
- Monthly reset for credit-based APIs
- Persist usage data across sessions
"""

import os
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from pathlib import Path
import threading

logger = logging.getLogger("tradingagents.dataflows.usage_tracker")


class UsageLimits:
    """Default limits for each API vendor."""
    
    # Perplexity: $5/month credit = ~50-100 requests
    PERPLEXITY_MONTHLY = 100
    PERPLEXITY_WARN_PERCENT = 80
    PERPLEXITY_DISCOVERY_RESERVE = 10
    
    # Finnhub: 60 requests per minute
    FINNHUB_PER_MINUTE = 60
    FINNHUB_DAILY = 50000  # Soft daily limit
    
    # Alpha Vantage: 25 requests per day (free tier)
    ALPHA_VANTAGE_DAILY = 25
    
    # yfinance: No hard limits, but be reasonable
    YFINANCE_DAILY = 10000


def _get_perplexity_config() -> Dict[str, Any]:
    try:
        from tradingagents.dataflows.config import get_config

        return dict(get_config().get("perplexity") or {})
    except Exception:
        return {}


def get_perplexity_monthly_limit() -> int:
    px = _get_perplexity_config()
    try:
        return int(px.get("monthly_limit") or UsageLimits.PERPLEXITY_MONTHLY)
    except (TypeError, ValueError):
        return UsageLimits.PERPLEXITY_MONTHLY


def get_discovery_reserve() -> int:
    px = _get_perplexity_config()
    try:
        return int(px.get("discovery_reserve", UsageLimits.PERPLEXITY_DISCOVERY_RESERVE))
    except (TypeError, ValueError):
        return UsageLimits.PERPLEXITY_DISCOVERY_RESERVE


def get_deep_ceiling() -> int:
    return max(0, get_perplexity_monthly_limit() - get_discovery_reserve())


class UsageTracker:
    """
    Track API usage across vendors to stay within limits.
    
    Usage:
        tracker = UsageTracker()
        
        if tracker.can_use("perplexity"):
            # Make API call
            tracker.record_use("perplexity")
        else:
            # Use fallback
            ...
    """
    
    def __init__(self, data_dir: str = None):
        """
        Initialize the usage tracker.
        
        Args:
            data_dir: Directory to persist usage data
        """
        if data_dir:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = Path(__file__).parent / "data_cache"
        
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.usage_file = self.data_dir / "api_usage.json"
        
        self._lock = threading.Lock()
        self._usage = self._load_usage()
        
        # Check for monthly reset
        self._check_monthly_reset()
    
    def _load_usage(self) -> Dict[str, Any]:
        """Load usage data from disk."""
        if self.usage_file.exists():
            try:
                with open(self.usage_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                pass
        
        return self._default_usage()
    
    def _default_usage(self) -> Dict[str, Any]:
        """Get default usage structure."""
        now = datetime.now()
        return {
            "month": now.strftime("%Y-%m"),
            "day": now.strftime("%Y-%m-%d"),
            "minute_window": now.strftime("%Y-%m-%d %H:%M"),
            "vendors": {
                "perplexity": {
                    "monthly": 0,
                    "daily": 0,
                    "monthly_auto_discovery": 0,
                    "monthly_discover": 0,
                    "monthly_methods": {},
                },
                "finnhub": {"daily": 0, "minute": 0},
                "alpha_vantage": {"daily": 0},
                "yfinance": {"daily": 0},
                "openai": {"daily": 0},
            }
        }

    def _ensure_perplexity_shape(self, usage: Dict[str, Any]) -> Dict[str, Any]:
        usage.setdefault("monthly", 0)
        usage.setdefault("daily", 0)
        usage.setdefault("monthly_auto_discovery", 0)
        usage.setdefault("monthly_discover", 0)
        usage.setdefault("monthly_methods", {})
        return usage
    
    def _save_usage(self) -> None:
        """Save usage data to disk."""
        with open(self.usage_file, 'w') as f:
            json.dump(self._usage, f, indent=2)
    
    def _check_monthly_reset(self) -> None:
        """Reset monthly counters if new month."""
        current_month = datetime.now().strftime("%Y-%m")
        if self._usage.get("month") != current_month:
            logger.info("New month detected, resetting monthly usage counters")
            self._usage["month"] = current_month
            for vendor in self._usage["vendors"].values():
                if "monthly" in vendor:
                    vendor["monthly"] = 0
                if "monthly_auto_discovery" in vendor:
                    vendor["monthly_auto_discovery"] = 0
                if "monthly_discover" in vendor:
                    vendor["monthly_discover"] = 0
                if "monthly_methods" in vendor:
                    vendor["monthly_methods"] = {}
            self._save_usage()
    
    def _check_daily_reset(self) -> None:
        """Reset daily counters if new day."""
        current_day = datetime.now().strftime("%Y-%m-%d")
        if self._usage.get("day") != current_day:
            logger.info("New day detected, resetting daily usage counters")
            self._usage["day"] = current_day
            for vendor in self._usage["vendors"].values():
                if "daily" in vendor:
                    vendor["daily"] = 0
            self._save_usage()
    
    def _check_minute_reset(self) -> None:
        """Reset per-minute counters if new minute."""
        current_minute = datetime.now().strftime("%Y-%m-%d %H:%M")
        if self._usage.get("minute_window") != current_minute:
            self._usage["minute_window"] = current_minute
            for vendor in self._usage["vendors"].values():
                if "minute" in vendor:
                    vendor["minute"] = 0

    def get_perplexity_monthly_used(self) -> int:
        with self._lock:
            self._check_monthly_reset()
            usage = self._ensure_perplexity_shape(
                self._usage["vendors"].setdefault("perplexity", {})
            )
            return int(usage.get("monthly") or 0)

    def get_deep_remaining(self) -> int:
        return max(0, get_deep_ceiling() - self.get_perplexity_monthly_used())

    def get_discover_used(self) -> int:
        with self._lock:
            self._check_monthly_reset()
            usage = self._ensure_perplexity_shape(
                self._usage["vendors"].setdefault("perplexity", {})
            )
            manual = int(usage.get("monthly_discover") or 0)
            auto = int(usage.get("monthly_auto_discovery") or 0)
            return manual + auto

    def can_use_deep_perplexity(self, count: int = 1) -> bool:
        """Deep analysis calls must stay below deep_ceiling (monthly - discovery reserve)."""
        with self._lock:
            return self._can_use_deep_perplexity_unlocked(count)

    def _can_use_deep_perplexity_unlocked(self, count: int = 1) -> bool:
        self._check_monthly_reset()
        usage = self._ensure_perplexity_shape(
            self._usage["vendors"].setdefault("perplexity", {})
        )
        monthly = int(usage.get("monthly") or 0)
        ceiling = get_deep_ceiling()
        if monthly + count > ceiling:
            logger.warning(
                "Perplexity deep ceiling reached (%d/%d; reserve=%d)",
                monthly,
                ceiling,
                get_discovery_reserve(),
            )
            return False
        return True

    def can_use_discover(self, count: int = 1) -> bool:
        """Discover / auto-discovery uses the reserved sub-budget."""
        with self._lock:
            return self._can_use_discover_unlocked(count)

    def _can_use_discover_unlocked(self, count: int = 1) -> bool:
        self._check_monthly_reset()
        usage = self._ensure_perplexity_shape(
            self._usage["vendors"].setdefault("perplexity", {})
        )
        monthly = int(usage.get("monthly") or 0)
        limit = get_perplexity_monthly_limit()
        reserve = get_discovery_reserve()
        manual = int(usage.get("monthly_discover") or 0)
        auto = int(usage.get("monthly_auto_discovery") or 0)
        discover_used = manual + auto
        if discover_used + count > reserve:
            logger.warning(
                "Discover reserve exhausted (%d/%d)",
                discover_used,
                reserve,
            )
            return False
        if monthly + count > limit:
            logger.warning(
                "Perplexity monthly limit reached (%d/%d)",
                monthly,
                limit,
            )
            return False
        return True
    
    def can_use(self, vendor: str, count: int = 1, *, usage_kind: str = "any") -> bool:
        """
        Check if we can make API calls to a vendor.
        
        Args:
            vendor: Vendor name (perplexity, finnhub, etc.)
            count: Number of calls to make
            usage_kind: For perplexity — ``deep``, ``discover``, or ``any``
            
        Returns:
            True if within limits
        """
        with self._lock:
            self._check_daily_reset()
            self._check_minute_reset()
            
            vendor_lower = vendor.lower()
            if vendor_lower not in self._usage["vendors"]:
                return True  # Unknown vendor, allow
            
            usage = self._usage["vendors"][vendor_lower]
            
            # Check limits based on vendor
            if vendor_lower == "perplexity":
                if usage_kind == "deep":
                    return self._can_use_deep_perplexity_unlocked(count)
                if usage_kind == "discover":
                    return self._can_use_discover_unlocked(count)

                monthly = usage.get("monthly", 0)
                limit = get_perplexity_monthly_limit()
                warn_at = limit * UsageLimits.PERPLEXITY_WARN_PERCENT / 100
                
                if monthly >= limit:
                    logger.warning(f"Perplexity monthly limit reached ({monthly}/{limit})")
                    return False
                if monthly >= warn_at:
                    remaining = limit - monthly
                    logger.warning(f"Perplexity at {monthly}/{limit} - {remaining} requests remaining this month")
                return True
            
            elif vendor_lower == "finnhub":
                minute = usage.get("minute", 0)
                if minute + count > UsageLimits.FINNHUB_PER_MINUTE:
                    logger.warning(f"Finnhub rate limit - {minute}/min, waiting...")
                    return False
                return True
            
            elif vendor_lower == "alpha_vantage":
                daily = usage.get("daily", 0)
                if daily + count > UsageLimits.ALPHA_VANTAGE_DAILY:
                    logger.warning(f"Alpha Vantage daily limit reached ({daily}/{UsageLimits.ALPHA_VANTAGE_DAILY})")
                    return False
                return True
            
            return True
    
    def record_use(self, vendor: str, count: int = 1, *, method: str = "") -> None:
        """
        Record API usage.
        
        Args:
            vendor: Vendor name
            count: Number of calls made
            method: Optional Perplexity method counter (e.g. get_deep_research)
        """
        with self._lock:
            self._check_daily_reset()
            self._check_minute_reset()
            
            vendor_lower = vendor.lower()
            if vendor_lower not in self._usage["vendors"]:
                self._usage["vendors"][vendor_lower] = {}
            
            usage = self._usage["vendors"][vendor_lower]
            
            # Increment appropriate counters
            if "monthly" in usage or vendor_lower == "perplexity":
                usage["monthly"] = usage.get("monthly", 0) + count
            
            if "daily" in usage or vendor_lower in ["finnhub", "alpha_vantage", "yfinance", "perplexity"]:
                usage["daily"] = usage.get("daily", 0) + count
            
            if "minute" in usage or vendor_lower == "finnhub":
                usage["minute"] = usage.get("minute", 0) + count

            if vendor_lower == "perplexity" and method:
                methods = usage.setdefault("monthly_methods", {})
                methods[method] = int(methods.get(method) or 0) + count
            
            self._save_usage()

    def record_discover_use(self, count: int = 1) -> None:
        """Record a manual Discover call in the discover sub-counter."""
        with self._lock:
            self._check_monthly_reset()
            usage = self._ensure_perplexity_shape(
                self._usage["vendors"].setdefault("perplexity", {})
            )
            usage["monthly_discover"] = int(usage.get("monthly_discover") or 0) + count
            self._save_usage()
    
    def can_use_auto_discovery(self, budget: int = 20) -> bool:
        """Check if auto-discovery has budget remaining this month.

        Both the auto-discovery sub-budget AND the overall Perplexity monthly
        limit must have capacity.  ``budget`` is the max auto-discovery calls
        per month (configurable, default 20).
        """
        with self._lock:
            self._check_monthly_reset()
            usage = self._usage["vendors"].get("perplexity", {})
            auto_used = usage.get("monthly_auto_discovery", 0)
            if auto_used >= budget:
                logger.warning(
                    "Auto-discovery monthly budget exhausted (%d/%d)",
                    auto_used, budget,
                )
                return False
            return self._can_use_discover_unlocked()

    def record_auto_discovery_use(self, count: int = 1) -> None:
        """Record a Perplexity call made by the auto-discovery system.

        Increments ONLY the ``monthly_auto_discovery`` sub-counter.  The
        overall ``monthly`` and ``daily`` counters are already incremented by
        :func:`~tradingagents.dataflows.interface.discover_opportunities` via
        ``record_use("perplexity")``, so double-counting here would inflate the
        global monthly limit check inside ``can_use_auto_discovery``.
        """
        with self._lock:
            self._check_monthly_reset()
            usage = self._usage["vendors"].setdefault("perplexity", {})
            usage["monthly_auto_discovery"] = usage.get("monthly_auto_discovery", 0) + count
            self._save_usage()
            logger.debug(
                "Auto-discovery sub-budget recorded: auto_discovery=%d, monthly=%d",
                usage["monthly_auto_discovery"], usage.get("monthly", 0),
            )

    def get_usage(self, vendor: str = None) -> Dict[str, Any]:
        """
        Get usage statistics.
        
        Args:
            vendor: Optional specific vendor, or None for all
            
        Returns:
            Usage statistics
        """
        with self._lock:
            self._check_daily_reset()
            self._check_monthly_reset()
            
            if vendor:
                vendor_lower = vendor.lower()
                usage = self._usage["vendors"].get(vendor_lower, {})
                
                # Add limit info
                if vendor_lower == "perplexity":
                    usage = self._ensure_perplexity_shape(dict(usage))
                    monthly = usage.get("monthly", 0)
                    auto_disc = usage.get("monthly_auto_discovery", 0)
                    manual_disc = usage.get("monthly_discover", 0)
                    limit = get_perplexity_monthly_limit()
                    reserve = get_discovery_reserve()
                    deep_ceiling = get_deep_ceiling()
                    return {
                        "vendor": vendor_lower,
                        "monthly_used": monthly,
                        "monthly_limit": limit,
                        "monthly_remaining": limit - monthly,
                        "deep_ceiling": deep_ceiling,
                        "deep_remaining": max(0, deep_ceiling - monthly),
                        "discovery_reserve": reserve,
                        "discover_used": manual_disc + auto_disc,
                        "percent_used": f"{monthly / limit * 100:.1f}%",
                        "monthly_auto_discovery": auto_disc,
                        "monthly_discover": manual_disc,
                        "monthly_methods": dict(usage.get("monthly_methods") or {}),
                    }
                elif vendor_lower == "finnhub":
                    return {
                        "vendor": vendor_lower,
                        "daily_used": usage.get("daily", 0),
                        "minute_used": usage.get("minute", 0),
                        "minute_limit": UsageLimits.FINNHUB_PER_MINUTE,
                    }
                elif vendor_lower == "alpha_vantage":
                    daily = usage.get("daily", 0)
                    return {
                        "vendor": vendor_lower,
                        "daily_used": daily,
                        "daily_limit": UsageLimits.ALPHA_VANTAGE_DAILY,
                        "daily_remaining": UsageLimits.ALPHA_VANTAGE_DAILY - daily,
                    }
                
                return {"vendor": vendor_lower, **usage}
            
            # Return all vendors
            return {
                "month": self._usage["month"],
                "day": self._usage["day"],
                "vendors": self._usage["vendors"]
            }
    
    def reset_vendor(self, vendor: str) -> None:
        """Reset usage for a specific vendor (for testing)."""
        with self._lock:
            vendor_lower = vendor.lower()
            if vendor_lower in self._usage["vendors"]:
                self._usage["vendors"][vendor_lower] = {}
                self._save_usage()


# Global tracker instance
_global_tracker: Optional[UsageTracker] = None


def get_tracker() -> UsageTracker:
    """Get the global usage tracker instance."""
    global _global_tracker
    if _global_tracker is None:
        _global_tracker = UsageTracker()
    return _global_tracker
