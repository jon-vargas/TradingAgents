"""
Alert Rule Evaluation Engine — evaluates alert conditions against live market data.

Supported alert types:
1. price_cross      — Price crosses above/below a threshold
2. volume_spike     — Volume exceeds N× the 20-day average
3. rsi_extreme      — RSI enters overbought/oversold territory
4. ma_crossover     — Moving average crossover detected
5. earnings_approaching — Earnings date within N days
6. rating_change    — Analyst recommendation key changes
7. short_interest_spike — Short interest increases significantly

Each evaluator returns (triggered: bool, message: str, data: dict).
"""

import logging
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger("tradingagents.screening.alert_evaluator")


class AlertEvaluator:
    """Evaluates alert rules against current market data."""

    # Supported alert types
    ALERT_TYPES = [
        "price_cross",
        "volume_spike",
        "rsi_extreme",
        "ma_crossover",
        "earnings_approaching",
        "rating_change",
        "short_interest_spike",
        "catalyst_approaching",
    ]

    ALERT_TYPE_DESCRIPTIONS = {
        "price_cross": "Triggers when price crosses above or below a target",
        "volume_spike": "Triggers when volume exceeds a multiple of the 20-day average",
        "rsi_extreme": "Triggers when RSI enters overbought or oversold territory",
        "ma_crossover": "Triggers when short-term MA crosses long-term MA",
        "earnings_approaching": "Triggers when earnings date is within N days",
        "rating_change": "Triggers when analyst recommendation key changes",
        "short_interest_spike": "Triggers when short interest increases significantly",
        "catalyst_approaching": "Triggers when an ex-dividend or split event is within N days",
    }

    # Default condition templates for each alert type
    CONDITION_DEFAULTS = {
        "price_cross": {"direction": "above", "target_price": 0.0},
        "volume_spike": {"multiplier": 2.0},
        "rsi_extreme": {"overbought": 70, "oversold": 30},
        "ma_crossover": {"short_period": 10, "long_period": 50},
        "earnings_approaching": {"days_threshold": 7},
        "rating_change": {"last_known_rating": ""},
        "short_interest_spike": {"increase_pct": 20},
        "catalyst_approaching": {"days_threshold": 14, "event_types": ["ex_dividend", "split"]},
    }

    def __init__(self, db=None):
        self.db = db
        self._data_cache: Dict[str, Dict[str, Any]] = {}  # Per-cycle cache
        self._rate_limited_this_cycle: bool = False
        self._rate_limited_count_this_cycle: int = 0

    def clear_cache(self):
        """Clear per-cycle data cache (call at start of each poll cycle)."""
        self._data_cache = {}
        self._rate_limited_this_cycle = False
        self._rate_limited_count_this_cycle = 0

    def note_rate_limited_skip(self) -> None:
        """Record that a ticker was skipped due to an open yfinance breaker."""
        self._rate_limited_this_cycle = True
        self._rate_limited_count_this_cycle += 1

    @property
    def rate_limited_this_cycle(self) -> bool:
        return self._rate_limited_this_cycle

    @property
    def rate_limited_count_this_cycle(self) -> int:
        return self._rate_limited_count_this_cycle

    def evaluate_rule(self, rule: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Evaluate a single alert rule against current data.

        Args:
            rule: Dict with keys: ticker, alert_type, condition

        Returns:
            (triggered, message, data) tuple
        """
        ticker = rule.get("ticker", "").upper()
        alert_type = rule.get("alert_type", "")
        condition = rule.get("condition", {})

        if alert_type not in self.ALERT_TYPES:
            return False, f"Unknown alert type: {alert_type}", {}

        # Fetch data for this ticker (cached per cycle)
        data = self._get_ticker_data(ticker)
        if data is None:
            return False, f"Could not fetch data for {ticker}", {}

        # Dispatch to type-specific evaluator
        evaluator = getattr(self, f"_check_{alert_type}", None)
        if evaluator is None:
            return False, f"No evaluator for {alert_type}", {}

        try:
            return evaluator(ticker, condition, data)
        except Exception as e:
            return False, f"Evaluation error for {ticker}/{alert_type}: {e}", {}

    def _get_ticker_data(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Fetch and cache ticker data for this evaluation cycle.

        Layering (cheapest first):
          1. per-cycle in-memory dict (one fetch per cycle for a ticker)
          2. centralized DataCache for history (TTL aligned with SCREENING_PRICES)
          3. ``yf.Ticker().history()`` guarded by the shared yfinance limiter
             (process-wide 429 circuit breaker + inter-call spacing)

        .info is pulled from the centralized DataCache (6h TTL) in
        ``get_ticker_info``.
        """
        if ticker in self._data_cache:
            return self._data_cache[ticker]

        try:
            from tradingagents.dataflows.cache import CacheConfig, get_cache
            from tradingagents.dataflows.yfinance_extended import get_ticker_info
            from tradingagents.dataflows.yfinance_limiter import get_yfinance_limiter

            info = get_ticker_info(ticker)
            limiter = get_yfinance_limiter()
            cache = get_cache()

            hist = cache.get("alert_hist_90d", ticker)

            if hist is None:
                if limiter.is_open():
                    # Breaker is open: skip this ticker silently; the monitor
                    # loop logs an aggregated summary once per cycle.
                    self.note_rate_limited_skip()
                    return None
                limiter.acquire()
                try:
                    end_date = datetime.now()
                    start_date = end_date - timedelta(days=90)
                    hist = yf.Ticker(ticker).history(
                        start=start_date, end=end_date, auto_adjust=True
                    )
                except Exception as exc:
                    if limiter.record_if_rate_limited(exc):
                        self.note_rate_limited_skip()
                        return None
                    raise
                limiter.record_success()
                if hist is None or hist.empty:
                    return None
                # Cache the history for SCREENING_PRICES TTL (1h) so subsequent
                # cycles in the same hour don't re-hit yfinance.
                try:
                    cache.set(
                        "alert_hist_90d",
                        ticker,
                        data=hist,
                        ttl=CacheConfig.SCREENING_PRICES,
                    )
                except Exception as cache_exc:
                    logger.debug("alert_hist_90d cache.set failed for %s: %s", ticker, cache_exc)

            if hist is None or getattr(hist, "empty", True):
                return None

            data = {
                "info": info,
                "hist": hist,
                "current_price": info.get("currentPrice")
                    or info.get("regularMarketPrice")
                    or (float(hist["Close"].iloc[-1]) if not hist.empty else None),
                "close": hist["Close"].dropna(),
                "volume": hist["Volume"].dropna(),
                "high": hist["High"].dropna(),
                "low": hist["Low"].dropna(),
            }
            self._data_cache[ticker] = data
            return data
        except Exception as e:
            logger.error("Failed to fetch data for %s: %s", ticker, e)
            return None

    # =========================================================================
    # Individual Alert Type Evaluators
    # =========================================================================

    def _check_price_cross(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if price crosses above/below a target."""
        direction = condition.get("direction", "above")
        target = condition.get("target_price", 0.0)
        current = data.get("current_price")

        if current is None or target == 0:
            return False, "", {}

        close = data["close"]
        if len(close) < 2:
            return False, "", {}

        prev_price = float(close.iloc[-2])

        if direction == "above":
            triggered = prev_price < target and current >= target
            msg = f"{ticker} crossed ABOVE ${target:.2f} (now ${current:.2f})"
        else:
            triggered = prev_price > target and current <= target
            msg = f"{ticker} crossed BELOW ${target:.2f} (now ${current:.2f})"

        return triggered, msg, {"current_price": current, "target": target, "direction": direction}

    def _check_volume_spike(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if volume exceeds N× the 20-day average."""
        multiplier = condition.get("multiplier", 2.0)
        volume = data["volume"]

        if len(volume) < 21:
            return False, "", {}

        avg_vol = float(volume.iloc[-21:-1].mean())
        current_vol = float(volume.iloc[-1])

        if avg_vol == 0:
            return False, "", {}

        ratio = current_vol / avg_vol
        triggered = ratio >= multiplier

        msg = ""
        if triggered:
            msg = f"{ticker} volume spike: {ratio:.1f}× average ({current_vol:,.0f} vs avg {avg_vol:,.0f})"

        return triggered, msg, {"current_volume": current_vol, "avg_volume": avg_vol, "ratio": round(ratio, 2)}

    def _check_rsi_extreme(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if RSI is in overbought/oversold territory."""
        overbought = condition.get("overbought", 70)
        oversold = condition.get("oversold", 30)
        close = data["close"]

        if len(close) < 15:
            return False, "", {}

        # Compute RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        current_rsi = float(rsi.iloc[-1])

        if pd.isna(current_rsi):
            return False, "", {}

        triggered = current_rsi >= overbought or current_rsi <= oversold
        zone = "OVERBOUGHT" if current_rsi >= overbought else "OVERSOLD" if current_rsi <= oversold else "neutral"

        msg = ""
        if triggered:
            msg = f"{ticker} RSI {zone}: {current_rsi:.1f} (thresholds: {oversold}/{overbought})"

        return triggered, msg, {"rsi": round(current_rsi, 2), "zone": zone}

    def _check_ma_crossover(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check for moving average crossover."""
        short_period = condition.get("short_period", 10)
        long_period = condition.get("long_period", 50)
        close = data["close"]

        if len(close) < long_period + 2:
            return False, "", {}

        short_ma = close.ewm(span=short_period, adjust=False).mean()
        long_ma = close.rolling(long_period).mean()

        current_diff = float(short_ma.iloc[-1] - long_ma.iloc[-1])
        prev_diff = float(short_ma.iloc[-2] - long_ma.iloc[-2])

        if pd.isna(current_diff) or pd.isna(prev_diff):
            return False, "", {}

        # Bullish crossover: short crosses above long
        bullish_cross = prev_diff <= 0 and current_diff > 0
        # Bearish crossover: short crosses below long
        bearish_cross = prev_diff >= 0 and current_diff < 0

        triggered = bullish_cross or bearish_cross
        cross_type = "BULLISH" if bullish_cross else "BEARISH" if bearish_cross else ""

        msg = ""
        if triggered:
            msg = f"{ticker} {cross_type} MA crossover: {short_period}EMA crossed {'above' if bullish_cross else 'below'} {long_period}SMA"

        return triggered, msg, {
            "cross_type": cross_type.lower(),
            "short_ma": round(float(short_ma.iloc[-1]), 2),
            "long_ma": round(float(long_ma.iloc[-1]), 2),
        }

    def _check_earnings_approaching(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if earnings date is within N days."""
        days_threshold = condition.get("days_threshold", 7)

        if self.db:
            try:
                events = self.db.get_upcoming_events(
                    ticker=ticker,
                    event_types=["earnings"],
                    max_days=int(days_threshold),
                )
                if events:
                    ev = events[0]
                    days_until = ev.get("days_to_event")
                    if days_until is not None and 0 <= int(days_until) <= days_threshold:
                        next_date = ev.get("event_date", "unknown")
                        msg = (
                            f"{ticker} earnings in {days_until} day"
                            f"{'s' if days_until != 1 else ''} (on {next_date})"
                        )
                        return True, msg, {
                            "days_until_earnings": days_until,
                            "next_earnings_date": next_date,
                            "source": "calendar",
                        }
            except Exception as e:
                logger.debug("Calendar earnings check failed for %s: %s", ticker, e)

        try:
            from tradingagents.dataflows.yfinance_extended import get_earnings_profile
            profile = get_earnings_profile(ticker)
            days_until = profile.get("days_until_earnings")

            if days_until is None or days_until < 0:
                return False, "", {}

            triggered = 0 <= days_until <= days_threshold
            msg = ""
            if triggered:
                next_date = profile.get("next_earnings_date", "unknown")
                msg = f"{ticker} earnings in {days_until} day{'s' if days_until != 1 else ''} (on {next_date})"

            return triggered, msg, {
                "days_until_earnings": days_until,
                "next_earnings_date": profile.get("next_earnings_date"),
                "earnings_beat_rate": profile.get("earnings_beat_rate_pct"),
                "source": "live",
            }
        except Exception as e:
            logger.debug("Earnings approaching check failed for ticker: %s", e)
            return False, "", {}

    def _check_rating_change(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if analyst recommendation key changed from last known."""
        last_known = condition.get("last_known_rating", "").lower()

        try:
            from tradingagents.dataflows.yfinance_extended import get_analyst_ratings
            ratings = get_analyst_ratings(ticker)
            current_rating = (ratings.get("recommendation_key") or "").lower()

            if not current_rating or not last_known:
                return False, "", {}

            triggered = current_rating != last_known
            msg = ""
            if triggered:
                msg = f"{ticker} analyst rating changed: {last_known.upper()} → {current_rating.upper()}"

            return triggered, msg, {
                "previous_rating": last_known,
                "current_rating": current_rating,
                "target_mean": ratings.get("target_mean_price"),
            }
        except Exception as e:
            logger.debug("Rating change check failed for ticker: %s", e)
            return False, "", {}

    def _check_short_interest_spike(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if short interest increased significantly."""
        increase_pct = condition.get("increase_pct", 20)
        info = data.get("info", {})

        shares_short = info.get("sharesShort", 0) or 0
        shares_short_prior = info.get("sharesShortPriorMonth", 0) or 0

        if shares_short_prior == 0 or shares_short == 0:
            return False, "", {}

        pct_change = ((shares_short - shares_short_prior) / shares_short_prior) * 100

        triggered = pct_change >= increase_pct
        msg = ""
        if triggered:
            short_pct = info.get("shortPercentOfFloat", 0) or 0
            msg = (
                f"{ticker} short interest spiked {pct_change:.1f}% "
                f"({shares_short_prior:,} → {shares_short:,}, "
                f"float: {short_pct*100:.1f}%)"
            )

        return triggered, msg, {
            "shares_short": shares_short,
            "shares_short_prior": shares_short_prior,
            "pct_change": round(pct_change, 2),
            "short_pct_float": info.get("shortPercentOfFloat"),
        }

    def _check_catalyst_approaching(
        self, ticker: str, condition: dict, data: dict
    ) -> Tuple[bool, str, dict]:
        """Check if a corporate-action catalyst (ex-dividend or split) is within N days."""
        days_threshold = int(condition.get("days_threshold", 14))
        event_types_wanted = set(condition.get("event_types", ["ex_dividend", "split"]))

        try:
            from tradingagents.dataflows.yfinance_extended import get_corporate_actions
            actions = get_corporate_actions(ticker)

            triggered_events = []

            if "ex_dividend" in event_types_wanted:
                days_away = actions.get("ex_div_days_to_event")
                if days_away is not None and 0 <= days_away <= days_threshold:
                    triggered_events.append({
                        "event_type": "ex_dividend",
                        "date": actions.get("next_ex_div_date"),
                        "days_to_event": days_away,
                    })

            if "split" in event_types_wanted:
                for split in actions.get("recent_splits", []):
                    try:
                        from datetime import datetime as _dt
                        split_dt = _dt.strptime(split["date"], "%Y-%m-%d")
                        days_away = (split_dt - _dt.now()).days
                        if 0 <= days_away <= days_threshold:
                            triggered_events.append({
                                "event_type": "split",
                                "date": split["date"],
                                "ratio": split.get("ratio"),
                                "days_to_event": days_away,
                            })
                    except Exception:
                        pass

            triggered = len(triggered_events) > 0
            msg = ""
            if triggered:
                parts = []
                for ev in triggered_events:
                    etype = ev["event_type"].replace("_", " ").title()
                    parts.append(f"{etype} in {ev['days_to_event']}d ({ev['date']})")
                msg = f"{ticker} catalyst: {'; '.join(parts)}"

            return triggered, msg, {"events": triggered_events}

        except Exception as e:
            logger.debug("Catalyst approaching check failed for %s: %s", ticker, e)
            return False, "", {}
