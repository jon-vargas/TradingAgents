"""
Smart Caching Layer - Minimize API calls with intelligent TTL management.

Features:
- Configurable TTL per data type
- Disk persistence for long-lived cache
- Memory cache for hot data
- Automatic cleanup of expired entries
- Cache statistics for monitoring
"""

import os
import json
import hashlib
import logging
import time
import tempfile
from datetime import datetime
from typing import Any, Optional, Dict
from pathlib import Path
import threading

logger = logging.getLogger("tradingagents.cache")


class CacheConfig:
    """TTL configuration for different data types (in seconds)."""
    
    # Price data - refresh frequently during market hours
    PRICE_DATA = 900  # 15 minutes
    
    # News - moderate refresh rate
    NEWS = 1800  # 30 minutes
    NEWS_GLOBAL = 3600  # 1 hour for global news
    
    # Fundamentals - quarterly data, refresh weekly
    FUNDAMENTALS = 604800  # 7 days (quarterly reports)
    BALANCE_SHEET = 604800  # 7 days
    INCOME_STATEMENT = 604800  # 7 days
    CASHFLOW = 604800  # 7 days
    
    # Technical indicators - refresh hourly
    TECHNICALS = 3600  # 1 hour
    
    # Perplexity research - expensive, cache longer
    PERPLEXITY_RESEARCH = 21600  # 6 hours
    PERPLEXITY_EARNINGS = 43200  # 12 hours
    PERPLEXITY_SEC = 604800  # 7 days
    PERPLEXITY_EARNINGS_TRANSCRIPT = 604800  # 7 days
    PERPLEXITY_SEC_SNAPSHOT = 604800  # 7 days
    
    # Tier 2: Analyst & screening data
    ANALYST_RATINGS = 21600  # 6 hours
    EARNINGS_CALENDAR = 21600  # 6 hours
    OWNERSHIP = 86400  # 24 hours (institutional data is quarterly)
    OPTIONS = 3600  # 1 hour (options change intraday)
    MACRO_SNAPSHOT = 3600  # 1 hour
    SCREENING_PRICES = 3600  # 1 hour
    EARLY_MOMENTUM_ENRICH = 86400  # 24 hours (filings/runway enrich)
    
    # Webapp ticker info (yf.Ticker.info) — static fields dominate (sector, name, summary);
    # price data available separately from OHLCV cache (1h TTL)
    TICKER_INFO = 21600  # 6 hours
    # Sparkline historical data — daily close, stable within the day
    SPARKLINE = 3600  # 1 hour
    # Sector momentum ETF data (macro-tactical overlay)
    SECTOR_MOMENTUM = 3600  # 1 hour

    # Institutional analysis enhancements
    INTRINSIC_VALUE = 86400     # 24 hours (DCF model, quarterly inputs)
    WEEKLY_TECHNICALS = 21600   # 6 hours (weekly candle data)

    # Default fallback
    DEFAULT = 3600  # 1 hour


class DataCache:
    """
    Smart caching system with TTL management and disk persistence.
    
    Usage:
        cache = DataCache()
        
        # Check cache first
        if cached := cache.get("news", "NFLX", "2024-01-01"):
            return cached
        
        # Fetch from API
        data = api.get_news("NFLX")
        
        # Store in cache
        cache.set("news", "NFLX", "2024-01-01", data)
    """
    
    # Maximum number of entries in the memory cache tier.
    # When exceeded, the oldest entries (by creation time) are evicted.
    MAX_MEMORY_ENTRIES = 2500

    def __init__(self, cache_dir: str = None, enable_disk: bool = True):
        """
        Initialize the cache.
        
        Args:
            cache_dir: Directory for disk cache (default: dataflows/data_cache)
            enable_disk: Whether to persist cache to disk
        """
        self.enable_disk = enable_disk
        
        # Memory cache: {key: {"data": ..., "expires": timestamp}}
        self._memory_cache: Dict[str, Dict] = {}
        self._lock = threading.Lock()
        
        # Disk cache directory
        if cache_dir:
            self.cache_dir = Path(cache_dir)
        else:
            self.cache_dir = Path(__file__).parent / "data_cache"
        
        if enable_disk:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Statistics
        self.stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "evictions": 0,
        }
    
    def _get_ttl(self, data_type: str) -> int:
        """Get TTL for a data type."""
        ttl_map = {
            "price": CacheConfig.PRICE_DATA,
            "stock_data": CacheConfig.PRICE_DATA,
            "news": CacheConfig.NEWS,
            "global_news": CacheConfig.NEWS_GLOBAL,
            "fundamentals": CacheConfig.FUNDAMENTALS,
            "balance_sheet": CacheConfig.BALANCE_SHEET,
            "income_statement": CacheConfig.INCOME_STATEMENT,
            "cashflow": CacheConfig.CASHFLOW,
            "indicators": CacheConfig.TECHNICALS,
            "technicals": CacheConfig.TECHNICALS,
            "perplexity_research": CacheConfig.PERPLEXITY_RESEARCH,
            "perplexity_earnings": CacheConfig.PERPLEXITY_EARNINGS,
            "perplexity_sec": CacheConfig.PERPLEXITY_SEC,
            "perplexity_earnings_transcript": CacheConfig.PERPLEXITY_EARNINGS_TRANSCRIPT,
            "perplexity_sec_snapshot": CacheConfig.PERPLEXITY_SEC_SNAPSHOT,
            # Tier 2 data types
            "analyst_ratings": CacheConfig.ANALYST_RATINGS,
            "earnings_calendar": CacheConfig.EARNINGS_CALENDAR,
            "earnings_calendar_v2": CacheConfig.EARNINGS_CALENDAR,
            "ownership": CacheConfig.OWNERSHIP,
            "options": CacheConfig.OPTIONS,
            "macro_snapshot": CacheConfig.MACRO_SNAPSHOT,
            "screening_prices": CacheConfig.SCREENING_PRICES,
            "screening_info": CacheConfig.SCREENING_PRICES,
            "screening_ohlcv_ticker": CacheConfig.SCREENING_PRICES,
            "screening_spy": CacheConfig.SCREENING_PRICES,
            "early_momentum_enrich": CacheConfig.EARLY_MOMENTUM_ENRICH,
            "risk_metrics": CacheConfig.FUNDAMENTALS,
            # Webapp endpoints
            "ticker_info": CacheConfig.TICKER_INFO,
            "ticker_info_full": CacheConfig.TICKER_INFO,  # full .info dict (backend)
            "sparkline": CacheConfig.SPARKLINE,
            # Macro-tactical overlay
            "sector_momentum": CacheConfig.SECTOR_MOMENTUM,
            # Institutional analysis enhancements
            "estimate_revisions": CacheConfig.EARNINGS_CALENDAR,
            "rating_changes": CacheConfig.ANALYST_RATINGS,
            "earnings_quality": CacheConfig.FUNDAMENTALS,
            "intrinsic_value": CacheConfig.INTRINSIC_VALUE,
            "sector_breadth": CacheConfig.SECTOR_MOMENTUM,
            "weekly_technicals": CacheConfig.WEEKLY_TECHNICALS,
            "scenario_analysis": CacheConfig.INTRINSIC_VALUE,
            "catalyst_pipeline": CacheConfig.PERPLEXITY_RESEARCH,
            "management_quality": CacheConfig.FUNDAMENTALS,
            "revenue_concentration": CacheConfig.FUNDAMENTALS,
        }
        return ttl_map.get(data_type.lower(), CacheConfig.DEFAULT)
    
    @staticmethod
    def _is_absent_key_arg(value: object) -> bool:
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
        return False

    def _make_key(self, data_type: str, *args) -> str:
        """Create a unique cache key from data type and arguments."""
        key_parts = [data_type] + [
            str(a) for a in args if not self._is_absent_key_arg(a)
        ]
        key_string = ":".join(key_parts)
        # Use hash for long keys
        if len(key_string) > 100:
            key_hash = hashlib.md5(key_string.encode()).hexdigest()[:16]
            return f"{data_type}:{key_hash}"
        return key_string
    
    def _get_disk_path(self, key: str) -> Path:
        """Get disk path for a cache key."""
        # Sanitize key for filesystem
        import re
        safe_key = re.sub(r'[<>:"/\\|?*]', "_", key)
        safe_key = safe_key.replace(":", "_").replace(" ", "_")
        if len(safe_key) > 200:
            safe_key = safe_key[:200]
        return self.cache_dir / f"{safe_key}.json"
    
    def get(self, data_type: str, *args) -> Optional[Any]:
        """
        Get data from cache if not expired.
        
        Args:
            data_type: Type of data (news, fundamentals, etc.)
            *args: Additional key components (ticker, date, etc.)
            
        Returns:
            Cached data or None if not found/expired
        """
        key = self._make_key(data_type, *args)
        now = time.time()
        
        # Check memory cache first
        with self._lock:
            if key in self._memory_cache:
                entry = self._memory_cache[key]
                if entry["expires"] > now:
                    self.stats["hits"] += 1
                    return entry["data"]
                else:
                    # Expired - remove from memory
                    del self._memory_cache[key]
                    self.stats["evictions"] += 1
        
        # Check disk cache
        if self.enable_disk:
            disk_path = self._get_disk_path(key)
            if disk_path.exists():
                try:
                    with open(disk_path, 'r') as f:
                        entry = json.load(f)
                    if entry["expires"] > now:
                        # Restore to memory cache
                        with self._lock:
                            self._memory_cache[key] = entry
                            self.stats["hits"] += 1
                        return entry["data"]
                    else:
                        # Expired - delete file
                        disk_path.unlink()
                        with self._lock:
                            self.stats["evictions"] += 1
                except (json.JSONDecodeError, KeyError, OSError, PermissionError, UnicodeDecodeError) as exc:
                    # Corrupted or unreadable cache file
                    logger.warning("Failed to read cache file %s: %s", disk_path, exc)
                    try:
                        disk_path.unlink()
                    except OSError:
                        pass
        
        with self._lock:
            self.stats["misses"] += 1
        return None
    
    def set(self, data_type: str, *args, data: Any, ttl: int = None) -> None:
        """
        Store data in cache.
        
        Args:
            data_type: Type of data (news, fundamentals, etc.)
            *args: Additional key components (ticker, date, etc.)
            data: Data to cache (must be JSON-serializable for disk cache)
            ttl: Optional custom TTL in seconds
        """
        key = self._make_key(data_type, *args)
        ttl = ttl or self._get_ttl(data_type)
        expires = time.time() + ttl
        
        entry = {
            "data": data,
            "expires": expires,
            "created": time.time(),
            "data_type": data_type,
        }
        
        # Store in memory (with LRU eviction when exceeding cap)
        with self._lock:
            self._memory_cache[key] = entry
            if len(self._memory_cache) > self.MAX_MEMORY_ENTRIES:
                # Evict oldest 10% by creation time to amortize cleanup cost
                evict_count = max(1, self.MAX_MEMORY_ENTRIES // 10)
                sorted_keys = sorted(
                    self._memory_cache,
                    key=lambda k: self._memory_cache[k].get("created", 0),
                )
                for ek in sorted_keys[:evict_count]:
                    del self._memory_cache[ek]
                self.stats["evictions"] += evict_count
        
        # Store on disk
        if self.enable_disk:
            disk_path = self._get_disk_path(key)
            try:
                disk_path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=str(disk_path.parent),
                    delete=False,
                    prefix=f"{disk_path.name}.tmp.",
                ) as tf:
                    json.dump(entry, tf)
                    tf.flush()
                    os.fsync(tf.fileno())
                    temp_path = tf.name
                os.replace(temp_path, disk_path)
            except (TypeError, ValueError):
                # Data not JSON-serializable (e.g. pandas DataFrames) — memory only, this is expected.
                # Clean up the temp file so it doesn't accumulate as orphaned .tmp.* files.
                try:
                    if "temp_path" in locals() and temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError:
                    pass
            except OSError as exc:
                logger.warning("Failed to write cache file %s: %s", disk_path, exc)
                try:
                    if "temp_path" in locals() and temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
                except OSError:
                    pass
        
        with self._lock:
            self.stats["sets"] += 1
    
    def invalidate(self, data_type: str, *args) -> bool:
        """
        Invalidate a specific cache entry.
        
        Args:
            data_type: Type of data
            *args: Additional key components
            
        Returns:
            True if entry was found and removed
        """
        key = self._make_key(data_type, *args)
        removed = False
        
        with self._lock:
            if key in self._memory_cache:
                del self._memory_cache[key]
                removed = True
        
        if self.enable_disk:
            disk_path = self._get_disk_path(key)
            if disk_path.exists():
                disk_path.unlink()
                removed = True
        
        return removed
    
    def invalidate_type(self, data_type: str) -> int:
        """
        Invalidate all cache entries of a specific type.
        
        Args:
            data_type: Type of data to invalidate
            
        Returns:
            Number of entries removed
        """
        count = 0
        prefix = f"{data_type}:"
        
        # Memory cache
        with self._lock:
            keys_to_remove = [k for k in self._memory_cache if k.startswith(prefix)]
            for key in keys_to_remove:
                del self._memory_cache[key]
                count += 1
        
        # Disk cache
        if self.enable_disk:
            for path in self.cache_dir.glob("*.json"):
                stem = path.stem
                if stem.startswith(f"{data_type}_") or stem == data_type:
                    path.unlink()
                    count += 1
        
        return count
    
    def clear(self) -> int:
        """
        Clear all cache entries.
        
        Returns:
            Number of entries removed
        """
        count = 0
        
        with self._lock:
            count += len(self._memory_cache)
            self._memory_cache.clear()
        
        if self.enable_disk:
            for path in self.cache_dir.glob("*.json"):
                path.unlink()
                count += 1
        
        return count
    
    def cleanup_expired(self) -> int:
        """
        Remove all expired entries.
        
        Returns:
            Number of entries removed
        """
        count = 0
        now = time.time()
        
        # Memory cache
        with self._lock:
            expired_keys = [
                k for k, v in self._memory_cache.items()
                if v["expires"] <= now
            ]
            for key in expired_keys:
                del self._memory_cache[key]
                count += 1
        
        # Disk cache — JSON entries with TTL metadata
        if self.enable_disk:
            for path in self.cache_dir.glob("*.json"):
                try:
                    with open(path, 'r') as f:
                        entry = json.load(f)
                    # Skip non-cache JSON payloads that coexist in this directory
                    # (e.g. usage tracking files without TTL metadata).
                    if not isinstance(entry, dict) or "expires" not in entry:
                        continue
                    if entry["expires"] <= now:
                        path.unlink()
                        count += 1
                except (json.JSONDecodeError, OSError, PermissionError, UnicodeDecodeError) as exc:
                    # Corrupted or unreadable cache file
                    logger.warning("Failed to read cache file %s: %s", path, exc)
                    try:
                        path.unlink()
                        count += 1
                    except OSError:
                        pass

            # Legacy CSV files (price data written by y_finance/stockstats) — age-based
            csv_cutoff = now - 7 * 86400  # 7 days
            for path in self.cache_dir.glob("*.csv"):
                try:
                    if path.stat().st_mtime < csv_cutoff:
                        path.unlink()
                        count += 1
                except OSError:
                    pass
        
        with self._lock:
            self.stats["evictions"] += count
        return count
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            memory_size = len(self._memory_cache)
        
        disk_size = 0
        if self.enable_disk:
            disk_size = len(list(self.cache_dir.glob("*.json")))
        
        total_requests = self.stats["hits"] + self.stats["misses"]
        hit_rate = (self.stats["hits"] / total_requests * 100) if total_requests > 0 else 0
        
        return {
            "memory_entries": memory_size,
            "disk_entries": disk_size,
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "hit_rate": f"{hit_rate:.1f}%",
            "sets": self.stats["sets"],
            "evictions": self.stats["evictions"],
        }


# Global cache instance
_global_cache: Optional[DataCache] = None


def get_cache() -> DataCache:
    """Get the global cache instance."""
    global _global_cache
    if _global_cache is None:
        _global_cache = DataCache()
    return _global_cache


def cached(data_type: str, ttl: int = None):
    """
    Decorator to cache function results.
    
    Usage:
        @cached("news")
        def get_news(ticker, start_date, end_date):
            ...
    
    Args:
        data_type: Type of data for TTL lookup
        ttl: Optional custom TTL
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            cache = get_cache()
            
            # Build cache key from function args
            cache_key_args = list(args) + [f"{k}={v}" for k, v in sorted(kwargs.items())]
            
            # Check cache
            if cached_data := cache.get(data_type, *cache_key_args):
                logger.debug("Cache HIT for %s:%s", data_type, cache_key_args)
                return cached_data
            
            # Call function
            logger.debug("Cache MISS for %s:%s", data_type, cache_key_args)
            result = func(*args, **kwargs)
            
            # Store in cache
            cache.set(data_type, *cache_key_args, data=result, ttl=ttl)
            
            return result
        return wrapper
    return decorator
