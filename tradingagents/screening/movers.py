"""
Movers Intelligence service built on deterministic Yahoo movers feeds.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import yfinance as yf

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.screening.engine import ScreeningEngine, ScreeningResult

logger = logging.getLogger("tradingagents.screening.movers")


_DEFAULT_SOURCES = ["day_gainers", "day_losers", "most_actives", "small_mid_caps"]
_KNOWN_SOURCES = set(_DEFAULT_SOURCES)


def _get_nested(payload: Any, path: List[str]) -> Any:
    value = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _signal_val(signals: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """Return a numeric signal value; None/missing/non-numeric → default."""
    raw = signals.get(key, default)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class MoversIntelligenceService:
    """Ingest movers feeds, run dual-horizon scans, and persist movers metadata."""

    def __init__(self, db, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or DEFAULT_CONFIG
        self.screening_cfg = self.config.get("screening", {})
        self.movers_cfg = self.screening_cfg.get("movers", {})

    def run_scan(
        self,
        include_losers: Optional[bool] = None,
        top_n: Optional[int] = None,
        asof_date: Optional[str] = None,
        source_lists: Optional[List[str]] = None,
        allow_preclose_skip: bool = True,
    ) -> Dict[str, Any]:
        tz_name = self.movers_cfg.get("market_timezone", "America/New_York")
        now_et = datetime.now(ZoneInfo(tz_name))
        asof_date = asof_date or now_et.date().isoformat()

        include_losers = self.movers_cfg.get("include_losers", True) if include_losers is None else bool(include_losers)
        top_n = int(top_n or self.movers_cfg.get("default_top_n", 25))
        top_n = max(1, min(top_n, 100))

        if (
            allow_preclose_skip
            and self.movers_cfg.get("post_close_guard_enabled", True)
            and not self._is_post_close(now_et)
        ):
            return {
                "skipped": True,
                "skip_reason": "preclose_guard",
                "message": "Movers scan skipped before regular market close (ET).",
                "as_of_et": now_et.isoformat(),
            }

        requested_sources = self._normalize_sources(source_lists, include_losers)
        raw_rows, source_errors = self._fetch_source_rows(requested_sources)
        normalized_rows = self._normalize_filter_dedupe(raw_rows, now_et=now_et, asof_date=asof_date)

        if not normalized_rows:
            raise ValueError("No movers passed filters after ingestion.")

        snapshot_id = f"{asof_date}:{int(now_et.timestamp())}"
        fetched_at = now_et.isoformat()
        if hasattr(self.db, "save_movers_snapshots"):
            try:
                self.db.save_movers_snapshots(
                    asof_date=asof_date,
                    fetched_at=fetched_at,
                    rows=normalized_rows,
                )
            except Exception as exc:
                logger.warning("Failed to persist movers snapshots: %s", exc)

        watchlist_id = self._upsert_dated_watchlist(asof_date, normalized_rows, now_et)
        tickers = [row["ticker"] for row in normalized_rows]
        source_map = {row["ticker"]: row for row in normalized_rows}

        short_preset = self.movers_cfg.get("presets", {}).get("short", "movers_swing_1to5d")
        medium_preset = self.movers_cfg.get("presets", {}).get("medium", "movers_swing_1to4w")
        engine = ScreeningEngine(config=self.config, db=self.db)

        base_criteria = {
            "strategy": "movers",
            "include_losers": include_losers,
            "top_n": top_n,
            "asof_date": asof_date,
            "source_lists": requested_sources,
            "snapshot_id": snapshot_id,
            "watchlist_id": watchlist_id,
        }

        short_results = engine.scan(
            tickers=tickers,
            date=asof_date,
            preset=short_preset,
            watchlist_id=watchlist_id,
            criteria_meta={**base_criteria, "horizon": "short", "preset": short_preset},
        )
        short_run_id = engine.last_run_id

        medium_results = engine.scan(
            tickers=tickers,
            date=asof_date,
            preset=medium_preset,
            watchlist_id=watchlist_id,
            criteria_meta={**base_criteria, "horizon": "medium", "preset": medium_preset},
        )
        medium_run_id = engine.last_run_id

        short_top, short_details = self._decorate_results(
            short_results, source_map, top_n, horizon="short", include_losers=include_losers
        )
        medium_top, medium_details = self._decorate_results(
            medium_results, source_map, top_n, horizon="medium", include_losers=include_losers
        )

        if short_run_id and hasattr(self.db, "save_movers_run_details"):
            self.db.save_movers_run_details(short_run_id, short_details)
        if medium_run_id and hasattr(self.db, "save_movers_run_details"):
            self.db.save_movers_run_details(medium_run_id, medium_details)

        degraded = bool(source_errors)
        return {
            "snapshot_id": snapshot_id,
            "watchlist_id": watchlist_id,
            "as_of_et": now_et.isoformat(),
            "degraded": degraded,
            "source_errors": source_errors,
            "runs": {
                "short": {
                    "run_id": short_run_id,
                    "preset": short_preset,
                    "top": short_top,
                },
                "medium": {
                    "run_id": medium_run_id,
                    "preset": medium_preset,
                    "top": medium_top,
                },
            },
        }

    def _normalize_sources(self, source_lists: Optional[List[str]], include_losers: bool) -> List[str]:
        # When no explicit selection is made, fall back to all defaults (which include small_mid_caps).
        # When the caller passes an explicit list we respect it exactly — no silent injections.
        if not source_lists:
            source_lists = list(_DEFAULT_SOURCES)
        normalized = []
        for source in source_lists:
            key = str(source or "").strip()
            if key in _KNOWN_SOURCES and key not in normalized:
                normalized.append(key)
        if include_losers and "day_losers" not in normalized:
            normalized.append("day_losers")
        if not include_losers:
            normalized = [s for s in normalized if s != "day_losers"]
        return normalized

    def _fetch_source_rows(self, source_lists: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        rows: List[Dict[str, Any]] = []
        source_errors: List[Dict[str, Any]] = []

        def _fetch_one(source: str) -> Tuple[str, List[Dict[str, Any]], Optional[Exception]]:
            try:
                return source, self._fetch_single_source(source), None
            except Exception as exc:
                return source, [], exc

        # Fetch all sources in parallel — each call hits a separate yfinance endpoint.
        workers = min(len(source_lists), 4)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_fetch_one, s): s for s in source_lists}
            results_by_source: Dict[str, List[Dict[str, Any]]] = {}
            for future in as_completed(futures):
                source, source_rows, exc = future.result()
                if exc is not None:
                    logger.warning("Movers source '%s' failed: %s", source, exc)
                    source_errors.append({"source": source, "error": str(exc)})
                else:
                    results_by_source[source] = source_rows

        # Preserve original source ordering for deterministic deduplication.
        for source in source_lists:
            for row in results_by_source.get(source, []):
                row = dict(row)
                row["_source_list"] = source
                rows.append(row)

        return rows, source_errors

    def _fetch_single_source(self, source: str) -> List[Dict[str, Any]]:
        if source == "small_mid_caps":
            payload = self._fetch_small_mid_caps_equity_query()
        else:
            payload = yf.screen(source)
        return self._extract_rows(payload)

    def _fetch_small_mid_caps_equity_query(self) -> Any:
        if not hasattr(yf, "EquityQuery"):
            return []
        min_cap = float(self.movers_cfg.get("min_market_cap", 500_000_000))
        min_vol = float(self.movers_cfg.get("min_avg_volume", 500_000))
        max_cap = float(self.movers_cfg.get("small_mid_max_market_cap", 25_000_000_000))

        # yfinance has changed EquityQuery signatures across versions, so try
        # a few known-compatible query shapes before falling back.
        builders = [
            lambda: yf.EquityQuery(
                "and",
                [
                    yf.EquityQuery("gt", ["intradaymarketcap", min_cap]),
                    yf.EquityQuery("lt", ["intradaymarketcap", max_cap]),
                    yf.EquityQuery("gt", ["dayvolume", min_vol]),
                ],
            ),
            lambda: yf.EquityQuery(
                "and",
                [
                    yf.EquityQuery("GT", "intradaymarketcap", min_cap),
                    yf.EquityQuery("LT", "intradaymarketcap", max_cap),
                    yf.EquityQuery("GT", "dayvolume", min_vol),
                ],
            ),
        ]
        last_error: Optional[Exception] = None
        for build in builders:
            try:
                query = build()
                return yf.screen(query)
            except Exception as exc:  # pragma: no cover - depends on yfinance version
                last_error = exc
                continue
        if last_error:
            raise last_error
        return []

    def _extract_rows(self, payload: Any) -> List[Dict[str, Any]]:
        if payload is None:
            return []
        if isinstance(payload, list):
            return [dict(x) for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for path in (["quotes"], ["data", "quotes"], ["finance", "result", "quotes"], ["items"], ["data"]):
                maybe = _get_nested(payload, path)
                if isinstance(maybe, list):
                    return [dict(x) for x in maybe if isinstance(x, dict)]
            return []
        try:
            import pandas as pd

            if isinstance(payload, pd.DataFrame):
                return payload.to_dict(orient="records")
        except Exception:
            pass
        return []

    @staticmethod
    def _pick_float(row: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
        for key in keys:
            val = row.get(key)
            if val is None:
                continue
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
        return default

    def _normalize_filter_dedupe(
        self,
        raw_rows: List[Dict[str, Any]],
        now_et: datetime,
        asof_date: str,
    ) -> List[Dict[str, Any]]:
        min_market_cap = float(self.movers_cfg.get("min_market_cap", 500_000_000))
        min_avg_volume = float(self.movers_cfg.get("min_avg_volume", 500_000))
        min_price = float(self.movers_cfg.get("min_price", 2.0))
        max_universe = int(self.movers_cfg.get("max_universe_size", 120))

        deduped: Dict[str, Dict[str, Any]] = {}
        for row in raw_rows:
            ticker = str(row.get("symbol") or row.get("ticker") or row.get("Symbol") or "").strip().upper()
            if not ticker:
                continue
            # Yahoo suffix listings (.NS, .IS, .L) are not a US tape desk.
            if self.movers_cfg.get("us_listings_only", True) and "." in ticker:
                continue

            market_cap = self._pick_float(row, ["marketCap", "intradaymarketcap", "market_cap"], 0.0)
            if market_cap and market_cap < min_market_cap:
                continue

            avg_volume = self._pick_float(
                row,
                ["averageDailyVolume3Month", "averageVolume", "avgVolume", "avg_daily_volume"],
                0.0,
            )
            volume = self._pick_float(row, ["regularMarketVolume", "volume", "dayvolume"], 0.0)
            if max(avg_volume, volume) < min_avg_volume:
                continue

            price = self._pick_float(row, ["regularMarketPrice", "lastPrice", "price"], 0.0)
            if price <= 0 or price < min_price:
                continue

            change_pct = self._pick_float(
                row,
                ["regularMarketChangePercent", "percentchange", "percentChange", "changePercent", "dayChangePercent"],
                0.0,
            )

            source = str(row.get("_source_list") or "unknown")
            normalized = {
                "asof_date": asof_date,
                "fetched_at": now_et.isoformat(),
                "source_list": source,
                "ticker": ticker,
                "price_change_pct": change_pct,
                "volume": volume or avg_volume,
                "market_cap": market_cap,
                "primary_bucket": source,
                "source_tags": [source],
                "payload_json": json.dumps(row, default=str),
            }

            existing = deduped.get(ticker)
            if existing is None:
                deduped[ticker] = normalized
                continue

            # Keep one row per ticker using highest absolute move for primary bucket.
            if abs(change_pct) > abs(existing.get("price_change_pct", 0.0)):
                existing["price_change_pct"] = change_pct
                existing["primary_bucket"] = source

            if market_cap and market_cap > existing.get("market_cap", 0.0):
                existing["market_cap"] = market_cap
            if volume and volume > existing.get("volume", 0.0):
                existing["volume"] = volume
            if source not in existing["source_tags"]:
                existing["source_tags"].append(source)

        rows = list(deduped.values())
        rows.sort(key=lambda r: abs(float(r.get("price_change_pct", 0.0))), reverse=True)
        rows = rows[:max_universe]
        for row in rows:
            row["source_tags"] = sorted(row["source_tags"])
        return rows

    def _upsert_dated_watchlist(self, asof_date: str, rows: List[Dict[str, Any]], now_et: datetime) -> int:
        prefix = self.movers_cfg.get("watchlist_name_prefix", "Movers")
        name = f"{prefix}: {asof_date}"
        description = "Auto-generated daily movers watchlist."
        tickers_csv = ",".join([r["ticker"] for r in rows])
        short_preset = self.movers_cfg.get("presets", {}).get("short", "movers_swing_1to5d")
        expires_at = (now_et + timedelta(days=int(self.movers_cfg.get("watchlist_retention_days", 14)))).isoformat()

        existing = None
        if hasattr(self.db, "get_watchlists"):
            for watchlist in self.db.get_watchlists():
                if watchlist.get("name") == name:
                    existing = watchlist
                    break

        if existing:
            self.db.update_watchlist(
                watchlist_id=int(existing["id"]),
                tickers=tickers_csv,
                default_preset=short_preset,
            )
            watchlist_id = int(existing["id"])
        else:
            watchlist_id = int(
                self.db.create_watchlist(
                    name=name,
                    description=description,
                    tickers=tickers_csv,
                    default_preset=short_preset,
                    default_investment_profile=None,
                )
            )

        if hasattr(self.db, "_connect"):
            with self.db._connect() as conn:
                conn.execute(
                    """UPDATE watchlists
                       SET source = ?, description = ?, expires_at = ?, updated_at = ?
                       WHERE id = ?""",
                    ("movers", description, expires_at, datetime.now().isoformat(), watchlist_id),
                )
                conn.commit()

        return watchlist_id

    def _decorate_results(
        self,
        results: List[ScreeningResult],
        source_map: Dict[str, Dict[str, Any]],
        top_n: int,
        horizon: str = "",
        include_losers: bool = True,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        rows: List[Dict[str, Any]] = []
        details: List[Dict[str, Any]] = []
        for result in results:
            ticker = str(result.ticker).upper()
            source_ctx = source_map.get(ticker, {})
            if self.movers_cfg.get("us_listings_only", True) and "." in ticker:
                continue
            if self._skip_implausible(ticker, source_ctx):
                continue
            catalyst_type, reason_codes = self._classify_result(result, source_ctx)
            change = float(source_ctx.get("price_change_pct") or 0.0)
            bucket = str(source_ctx.get("primary_bucket") or "unknown")
            tags = list(source_ctx.get("source_tags") or [])
            if change < 0 and bucket == "day_gainers":
                bucket = "day_losers" if "day_losers" in tags else "downside_tape"
            elif change > 0 and bucket == "day_losers":
                bucket = "day_gainers" if "day_gainers" in tags else "upside_tape"
            row = {
                "ticker": ticker,
                "composite_score": float(result.composite_score),
                "direction": getattr(result, "direction", "") or "",
                "catalyst_type": catalyst_type,
                "reason_codes": reason_codes,
                "primary_bucket": bucket,
                "source_tags": tags,
                "price_change_pct": change,
                "horizon": horizon,
            }
            details.append(
                {
                    "ticker": ticker,
                    "catalyst_type": catalyst_type,
                    "reason_codes": reason_codes,
                    "primary_bucket": row["primary_bucket"],
                    "source_tags": row["source_tags"],
                    "price_change_pct": row["price_change_pct"],
                    "direction": getattr(result, "direction", "") or "",
                    "horizon": horizon,
                    "score_breakdown_json": {
                        "composite_score": float(result.composite_score),
                        "direction": result.direction,
                        "signals": result.signals,
                        "price_change_pct": change,
                        "market_cap": source_ctx.get("market_cap") or 0.0,
                        "horizon": horizon,
                    },
                }
            )
            rows.append(row)
        details.sort(key=lambda d: self.rank_key(d))
        return self.select_board(rows, top_n, include_losers=include_losers), details

    def is_board_eligible(self, ticker: str, row: Optional[Dict[str, Any]] = None) -> bool:
        """Shared GET/decorate gate: US listings and no Yahoo mega collisions."""
        sym = str(ticker or "").strip().upper()
        if not sym:
            return False
        if self.movers_cfg.get("us_listings_only", True) and "." in sym:
            return False
        ctx = dict(row or {})
        breakdown = ctx.get("score_breakdown_json") or {}
        if isinstance(breakdown, dict) and not ctx.get("market_cap"):
            ctx["market_cap"] = breakdown.get("market_cap")
        return not self._skip_implausible(sym, ctx)

    @classmethod
    def select_board(
        cls,
        rows: List[Dict[str, Any]],
        top_n: int,
        include_losers: bool = True,
    ) -> List[Dict[str, Any]]:
        """Build the desk board: follow-through first, with a reserved loser pocket."""
        ordered = sorted(rows, key=cls.rank_key)
        top_n = max(1, int(top_n or 25))
        if not include_losers:
            return [r for r in ordered if float(r.get("price_change_pct") or 0.0) >= 0][:top_n]

        downs = [r for r in ordered if float(r.get("price_change_pct") or 0.0) < 0]
        ups = [r for r in ordered if float(r.get("price_change_pct") or 0.0) >= 0]
        # Pocket is for actual down prints, not slightly-red names that scored well.
        downs.sort(
            key=lambda r: (
                -abs(float(r.get("price_change_pct") or 0.0)),
                -float(r.get("composite_score") or 0.0),
            )
        )
        loser_slots = min(len(downs), max(3, top_n // 5))
        board = ups[: max(0, top_n - loser_slots)] + downs[:loser_slots]
        board.sort(key=cls.rank_key)
        return board

    _SORT_LAST = frozenset({"low_quality_spike"})
    _SORT_LATE = frozenset({"overextended_rally", "downside_move"})

    @classmethod
    def rank_key(cls, row: Dict[str, Any]) -> Tuple[int, float]:
        """Tape follow-through first; soft downs/extended next; junk last.

        Unclassified large prints still compete — a +14% name with no named
        catalyst is more useful than a quiet technical label. Only
        low_quality_spike is sorted off the board.
        """
        cat = str(row.get("catalyst_type") or "unclassified")
        lane = 2 if cat in cls._SORT_LAST else (1 if cat in cls._SORT_LATE else 0)
        composite = float(row.get("composite_score") or 0.0)
        move = abs(float(row.get("price_change_pct") or 0.0))
        horizon = str(row.get("horizon") or "")
        if not horizon:
            breakdown = row.get("score_breakdown_json") or {}
            if isinstance(breakdown, dict):
                horizon = str(breakdown.get("horizon") or "")
        if horizon == "short":
            follow = composite * 0.75 + min(move, 15.0) * 2.4
        elif horizon == "medium":
            follow = composite * 1.05 + min(move, 12.0) * 1.1
        else:
            follow = composite + min(move, 12.0) * 1.6
        return (lane, -follow)

    @staticmethod
    def source_map_from_snapshot_rows(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Merge persisted snapshot rows the same way ingest dedupes live tape."""
        merged: Dict[str, Dict[str, Any]] = {}
        for row in rows or []:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            source = str(row.get("source_list") or row.get("primary_bucket") or "unknown")
            try:
                change = float(row.get("price_change_pct") or 0.0)
            except (TypeError, ValueError):
                change = 0.0
            try:
                market_cap = float(row.get("market_cap") or 0.0)
            except (TypeError, ValueError):
                market_cap = 0.0
            existing = merged.get(ticker)
            if existing is None:
                merged[ticker] = {
                    "ticker": ticker,
                    "price_change_pct": change,
                    "primary_bucket": source,
                    "source_tags": [source] if source else [],
                    "market_cap": market_cap,
                }
                continue
            if abs(change) > abs(float(existing.get("price_change_pct") or 0.0)):
                existing["price_change_pct"] = change
                existing["primary_bucket"] = source
            if market_cap > float(existing.get("market_cap") or 0.0):
                existing["market_cap"] = market_cap
            tags = existing.setdefault("source_tags", [])
            if source and source not in tags:
                tags.append(source)
        return merged

    @staticmethod
    def _skip_implausible(ticker: str, source_ctx: Optional[Dict[str, Any]] = None) -> bool:
        from tradingagents.screening.discovery import is_implausible_mega_identity

        return is_implausible_mega_identity(
            ticker,
            (source_ctx or {}).get("market_cap"),
            str((source_ctx or {}).get("market_cap_tier") or ""),
        )

    def _classify_result(
        self,
        result: ScreeningResult,
        source_ctx: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[str]]:
        signals = result.signals or {}
        sig = _signal_val
        reasons: List[str] = []
        change = float((source_ctx or {}).get("price_change_pct") or 0.0)

        if change <= -3.0:
            if sig(signals, "volume_surge") >= 0.70 and sig(signals, "rsi_oversold") >= 0.55:
                return "capitulation_washout", ["volume_surge", "rsi_oversold", "downside_print"]
            if sig(signals, "volume_surge") >= 0.80 and sig(signals, "smart_money") <= 0.25:
                return "forced_liquidation", ["volume_surge", "weak_smart_money", "downside_print"]
            return "downside_move", ["downside_print"]

        # Missing PEAD (None) must not count as supportive. _signal_val maps
        # None → 0.0, so >= 0.55 is already false — keep that contract explicit.
        if sig(signals, "earnings_proximity") >= 0.65 and sig(signals, "pead_drift") >= 0.55:
            reasons.extend(["earnings_proximity_high", "pead_drift_support"])
            return "earnings_drift", reasons

        if sig(signals, "rating_momentum") >= 0.65:
            reasons.append("rating_momentum")
            if sig(signals, "price_vs_target") >= 0.75:
                reasons.append("price_vs_target")
            return "analyst_catalyst", reasons

        if (
            change >= 1.5
            and sig(signals, "relative_strength") >= 0.70
            and sig(signals, "ma_crossover") >= 0.60
            and sig(signals, "trend_strength") >= 0.55
        ):
            reasons.extend(["relative_strength", "ma_crossover", "trend_strength"])
            return "momentum_breakout", reasons

        if sig(signals, "rsi_overbought") >= 0.70 and sig(signals, "volume_surge") >= 0.70:
            reasons.extend(["rsi_overbought", "volume_surge"])
            return "overextended_rally", reasons

        if (
            sig(signals, "volume_surge") >= 0.80
            and sig(signals, "smart_money") <= 0.25
            and sig(signals, "estimate_momentum") <= 0.40
        ):
            reasons.extend(["volume_surge", "weak_smart_money", "weak_estimate_momentum"])
            return "low_quality_spike", reasons

        if change >= 2.0 and sig(signals, "estimate_momentum") >= 0.85:
            reasons.extend(["estimate_momentum", "up_print"])
            return "estimate_revision", reasons

        if change >= 5.0:
            return "upside_move", ["large_up_print"]

        return "unclassified", reasons or ["no_rule_match"]

    @staticmethod
    def _is_post_close(now_et: datetime) -> bool:
        # Weekend: Friday's tape is closed. Allow research scans.
        if now_et.weekday() >= 5:
            return True
        if now_et.hour > 16:
            return True
        if now_et.hour == 16 and now_et.minute >= 15:
            return True
        return False
