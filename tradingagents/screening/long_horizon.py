"""
Long-horizon institutional research workflow.

Deterministic-first pipeline:
1) build policy-constrained universe
2) run screening engine with long-horizon preset
3) apply deterministic risk overlay + capped allocation
4) optionally generate top-N underwriting packets
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from tradingagents.dataflows.risk_metrics import compute_risk_metrics
from tradingagents.dataflows.yfinance_extended import get_macro_snapshot, get_ticker_info
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.research import ResearchAgent
from tradingagents.screening.engine import ScreeningEngine
from tradingagents.screening.macro_overlay import compute_sector_momentum
from tradingagents.screening.ticker_resolver import resolve_and_cache

logger = logging.getLogger("tradingagents.screening.long_horizon")


def _json_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_symbol(raw: str) -> str:
    return str(raw or "").strip().upper()


def _utc_now() -> datetime:
    """Timezone-aware UTC now helper."""
    return datetime.now(timezone.utc)


def _utc_iso() -> str:
    return _utc_now().isoformat()


def _looks_leveraged_or_inverse(info: Dict[str, Any]) -> bool:
    """Best-effort leveraged/inverse detector from metadata strings."""
    text = " ".join(
        str(info.get(k) or "")
        for k in ("longName", "shortName", "fundFamily", "category", "quoteType")
    ).lower()
    if not text.strip():
        return False
    leverage_signals = (
        "2x",
        "3x",
        "ultra",
        "ultrapro",
        "leveraged",
        "inverse",
        "daily x",
    )
    return any(sig in text for sig in leverage_signals)


class LongHorizonService:
    def __init__(self, db, config: Optional[Dict[str, Any]] = None):
        self.db = db
        self.config = config or DEFAULT_CONFIG
        self.screen_cfg = self.config.get("screening", {})
        self.long_cfg = self.screen_cfg.get("long_horizon", {})
        self.presets_cfg = self.screen_cfg.get("presets", {})
        self.last_run_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def run_scan(
        self,
        horizon: str,
        top_n: int = 30,
        watchlist_id: Optional[int] = None,
        tickers: Optional[List[str]] = None,
        date: Optional[str] = None,
        use_expanded_universe: bool = False,
    ) -> Dict[str, Any]:
        started = time.time()
        horizon_key = str(horizon or "").strip()
        horizon_cfg = self.long_cfg.get("horizons", {}).get(horizon_key)
        if not horizon_cfg:
            raise ValueError("Unsupported horizon. Use long_6to12m or long_12to36m.")

        universe_raw, universe_meta = self._resolve_universe_inputs(
            watchlist_id=watchlist_id,
            tickers=tickers,
            use_expanded_universe=use_expanded_universe,
        )
        if not universe_raw:
            raise ValueError("No tickers available after universe resolution.")

        policy = self.long_cfg.get("universe_policy", {})
        eligible, excluded = self._apply_universe_policy(universe_raw, policy=policy)
        if not eligible:
            raise ValueError("No eligible tickers after universe policy filters.")
        exclusion_counts = dict(Counter(code for row in excluded for code in row.get("reason_codes", [])))
        policy_summary = {
            "input_count": len(universe_raw),
            "eligible_count": len(eligible),
            "excluded_count": len(excluded),
            "exclusion_reason_counts": exclusion_counts,
        }

        preset = horizon_cfg.get("preset")
        if preset not in self.presets_cfg:
            raise ValueError(f"Preset not found for horizon: {preset}")

        scan_date = date or _utc_now().strftime("%Y-%m-%d")
        exec_cfg = self.long_cfg.get("execution_profile", {})
        expanded_mode = bool(use_expanded_universe and universe_meta.get("source") == "expanded_universe")
        fast_path_enabled = bool(exec_cfg.get("expanded_fast_path_enabled", True))
        two_pass_min_universe_size = max(2, int(exec_cfg.get("two_pass_min_universe_size", 180)))
        fast_top_k = max(2, int(exec_cfg.get("fast_pass_top_k", 120)))

        engine = ScreeningEngine(config=self.config, db=self.db)
        deep_input = eligible
        execution_meta: Dict[str, Any] = {
            "mode": "single_pass",
            "expanded_fast_path_enabled": fast_path_enabled,
            "expanded_mode_input": expanded_mode,
            "two_pass_min_universe_size": two_pass_min_universe_size,
            "eligible_count": len(eligible),
            "fast_pass_top_k": fast_top_k,
        }

        if fast_path_enabled and len(eligible) >= two_pass_min_universe_size and len(eligible) > fast_top_k:
            fast_started = time.time()
            fast_results = engine.scan(
                tickers=eligible,
                date=scan_date,
                preset=preset,
                watchlist_id=None,  # long-horizon persists final pass only
                enable_enhanced=bool(exec_cfg.get("fast_pass_enable_enhanced", False)),
                funnel_cutoff_pct=float(exec_cfg.get("fast_pass_funnel_cutoff_pct", 0.40)),
                chunk_sleep_seconds=float(exec_cfg.get("fast_pass_chunk_sleep_seconds", 0.0)),
                criteria_meta={
                    "strategy": "long_horizon",
                    "horizon": horizon_key,
                    "scan_stage": "expanded_fast_pass",
                },
            )
            execution_meta["fast_pass_duration_seconds"] = round(time.time() - fast_started, 3)
            fast_rows = sorted([asdict(r) for r in fast_results], key=lambda r: float(r.get("composite_score", 0)), reverse=True)
            deep_input = [str(r.get("ticker", "")).upper() for r in fast_rows[:fast_top_k] if str(r.get("ticker", "")).strip()]
            deep_input = list(dict.fromkeys(deep_input))
            execution_meta.update(
                {
                    "mode": "two_pass",
                    "fast_pass_input_count": len(eligible),
                    "fast_pass_result_count": len(fast_rows),
                    "deep_pass_input_count": len(deep_input),
                }
            )
            if not deep_input:
                deep_input = eligible
                execution_meta["mode"] = "single_pass_fallback"

        deep_started = time.time()
        scan_results = engine.scan(
            tickers=deep_input,
            date=scan_date,
            preset=preset,
            watchlist_id=None,  # long-horizon persists with explicit strategy metadata
            enable_enhanced=bool(exec_cfg.get("deep_pass_enable_enhanced", True)),
            funnel_cutoff_pct=float(exec_cfg.get("deep_pass_funnel_cutoff_pct", self.screen_cfg.get("funnel_cutoff_pct", 0.60))),
            enhanced_top_pct=float(exec_cfg.get("deep_pass_enhanced_top_pct", self.screen_cfg.get("enhanced_top_pct", 0.30))),
            enhanced_min_count=int(exec_cfg.get("deep_pass_enhanced_min_count", self.screen_cfg.get("enhanced_min_count", 15))),
            chunk_sleep_seconds=float(exec_cfg.get("deep_pass_chunk_sleep_seconds", self.screen_cfg.get("chunk_sleep_seconds", 1.0))),
            criteria_meta={
                "strategy": "long_horizon",
                "horizon": horizon_key,
                "scan_stage": "final_deep_pass",
                "universe_policy_hash": _json_hash(policy),
                "requested_top_n": int(top_n),
            },
        )
        execution_meta["deep_pass_duration_seconds"] = round(time.time() - deep_started, 3)

        criteria_payload = {
            "strategy": "long_horizon",
            "horizon": horizon_key,
            "preset": preset,
            "weights": self.presets_cfg[preset].get("weights", {}),
            "universe_policy_hash": _json_hash(policy),
            "watchlist_id": watchlist_id,
            "custom_universe": watchlist_id is None,
            "requested_top_n": int(top_n),
            "execution_mode": execution_meta.get("mode"),
            "execution_profile": {
                "fast_pass_top_k": fast_top_k,
                "deep_pass_enhanced_top_pct": float(exec_cfg.get("deep_pass_enhanced_top_pct", self.screen_cfg.get("enhanced_top_pct", 0.30))),
                "deep_pass_funnel_cutoff_pct": float(exec_cfg.get("deep_pass_funnel_cutoff_pct", self.screen_cfg.get("funnel_cutoff_pct", 0.60))),
            },
        }
        run_id = self.db.save_screening_run(
            watchlist_id=watchlist_id,
            criteria=json.dumps(criteria_payload),
            ticker_count=len(eligible),
            results_count=len(scan_results),
        )
        self.db.save_screening_results(run_id, [asdict(r) for r in scan_results])

        self.last_run_id = run_id
        rows = [asdict(r) for r in scan_results]
        rows = sorted(rows, key=lambda r: float(r.get("composite_score", 0)), reverse=True)

        overlayed, allocation = self._apply_risk_overlay_and_allocate(
            rows, top_n=max(1, int(top_n)), preset=preset,
        )

        run_meta = {
            "run_id": run_id,
            "strategy": "long_horizon",
            "horizon": horizon_key,
            "preset": preset,
            "weights_hash": _json_hash(self.presets_cfg[preset].get("weights", {})),
            "overlay_hash": _json_hash(self.long_cfg.get("allocation", {})),
            "universe_policy_hash": _json_hash(policy),
            "provenance_version": "long_horizon_v1",
            "underwriting_model_version": self.long_cfg.get("underwriting", {}).get("model_version", "research_v1"),
            "underwriting_template_version": self.long_cfg.get("underwriting", {}).get("template_version", "long_horizon_underwriting_v1"),
            "regime_snapshot": self._regime_snapshot(),
            "universe": universe_meta,
            "policy_summary": policy_summary,
            "execution_profile": execution_meta,
            "started_at": _utc_iso(),
            "duration_seconds": round(time.time() - started, 3),
        }

        self.db.save_long_horizon_run_details(run_id=run_id, details=overlayed)
        try:
            if universe_meta.get("source") == "expanded_universe":
                self.db.record_runtime_metric(
                    "expanded_scan_duration_seconds",
                    float(run_meta.get("duration_seconds") or 0.0),
                    context={
                        "horizon": horizon_key,
                        "eligible_count": int(policy_summary.get("eligible_count") or 0),
                        "run_id": int(run_id),
                    },
                )
        except Exception:
            pass

        # Reuse the betas already fetched during the allocation pass to avoid a
        # second round of yfinance calls inside compute_portfolio_risk_summary.
        beta_map: Dict[str, Optional[float]] = {
            str(e.get("ticker", "")).upper(): e.get("beta")
            for e in overlayed
            if str(e.get("ticker", "")).strip()
        }

        # Portfolio-level risk summary (factor exposures, sector concentration, weighted beta)
        portfolio_risk = self.compute_portfolio_risk_summary(
            allocation=allocation,
            scan_results=rows,
            beta_map=beta_map,
        )
        run_meta["portfolio_risk"] = portfolio_risk
        self.db.save_long_horizon_run_meta(run_id=run_id, meta=run_meta)

        return {
            "run_id": run_id,
            "as_of": _utc_iso(),
            "horizon": horizon_key,
            "preset": preset,
            "top": overlayed[: max(1, int(top_n))],
            "excluded": excluded,
            "allocation": allocation,
            "portfolio_risk": portfolio_risk,
            "run_snapshot_hash": _json_hash(run_meta),
            "meta": run_meta,
            "universe": universe_meta,
            "policy_summary": policy_summary,
        }

    def underwrite_top(
        self,
        run_id: int,
        top_n: int = 10,
        date: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        cfg = self.long_cfg.get("underwriting", {})
        max_jobs = int(cfg.get("max_underwrite_jobs_per_run", 12))
        top_n = max(1, min(int(top_n), max_jobs))
        llm_top_k = max(0, min(int(cfg.get("llm_augment_top_k", top_n)), top_n))
        llm_call_timeout = max(1.0, float(cfg.get("llm_call_timeout_seconds", 45)))

        run = self.db.get_screening_run(run_id)
        if not run:
            raise ValueError("run_not_found")
        criteria = self._parse_criteria(run.get("criteria"))
        if criteria.get("strategy") != "long_horizon":
            raise ValueError("not_long_horizon_run")

        results = self.db.get_screening_results(run_id)
        if not results:
            return {"run_id": run_id, "count": 0, "items": [], "degraded": True, "error_code": "insufficient_data"}

        details = self.db.get_long_horizon_run_details(run_id=run_id) if hasattr(self.db, "get_long_horizon_run_details") else []
        allocated = [
            d for d in (details or [])
            if float(d.get("target_weight") or 0) > 0
        ]
        if allocated:
            by_ticker = {str(r.get("ticker", "")).upper(): r for r in results}
            top_rows = []
            for d in sorted(allocated, key=lambda r: float(r.get("target_weight") or 0), reverse=True)[:top_n]:
                ticker = str(d.get("ticker", "")).upper()
                merged = dict(by_ticker.get(ticker) or {})
                merged.update(d)
                top_rows.append(merged)
            if not top_rows:
                top_rows = sorted(results, key=lambda r: float(r.get("composite_score", 0)), reverse=True)[:top_n]
        else:
            top_rows = sorted(results, key=lambda r: float(r.get("composite_score", 0)), reverse=True)[:top_n]
        timeout_sec = int(cfg.get("underwrite_timeout_seconds", 120))
        retry_budget = int(cfg.get("underwrite_retry_budget", 1))
        token_budget = int(cfg.get("underwrite_token_budget_per_run", 18000))
        model_version = str(cfg.get("model_version", "research_v1"))
        template_version = str(cfg.get("template_version", "long_horizon_underwriting_v1"))

        # Deterministic fallback packet generator with bounded concurrent underwriting.
        packets_by_ticker: Dict[str, Dict[str, Any]] = {}
        degraded = False
        started = time.time()
        deadline = started + float(timeout_sec)
        budget_used = 0
        budget_lock = threading.Lock()
        max_concurrency = max(1, int(cfg.get("max_underwrite_concurrency", 3)))
        allow_llm = bool(cfg.get("enabled", True))

        # First, deterministically pre-fill rows outside llm_top_k with degraded packets.
        llm_rows: List[Tuple[int, Dict[str, Any]]] = []
        for idx, row in enumerate(top_rows):
            ticker = str(row.get("ticker", "")).upper()
            if not ticker:
                continue
            if idx >= llm_top_k:
                degraded = True
                packets_by_ticker[ticker] = self._degraded_packet(
                    ticker=ticker,
                    run_id=run_id,
                    error_code="llm_skipped_cap",
                    note=f"LLM augmentation cap reached ({llm_top_k}); deterministic packet returned.",
                )
            else:
                llm_rows.append((idx, row))

        def _process_row(row: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal budget_used
            ticker = str(row.get("ticker", "")).upper()
            if not ticker:
                return {}

            # Fast budget check before doing any expensive work.
            with budget_lock:
                if budget_used >= token_budget:
                    return self._degraded_packet(
                        ticker=ticker,
                        run_id=run_id,
                        error_code="budget_exceeded",
                        note="Token budget exceeded; deterministic packet returned.",
                    )

            attempt = 0
            packet: Optional[Dict[str, Any]] = None
            while attempt <= retry_budget:
                attempt += 1
                try:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        raise TimeoutError("underwrite timeout")
                    per_call_timeout = min(float(llm_call_timeout), max(1.0, remaining))
                    packet = self._call_with_timeout(
                        lambda: self._build_underwriting_packet(
                            row=row,
                            run_id=run_id,
                            date=date,
                            allow_llm=allow_llm,
                        ),
                        timeout_seconds=per_call_timeout,
                    )
                    break
                except Exception as exc:  # pragma: no cover - fallback path
                    if attempt > retry_budget:
                        packet = self._degraded_packet(
                            ticker=ticker,
                            run_id=run_id,
                            error_code="dependency_error",
                            note=f"Underwriting fallback used: {str(exc)[:200]}",
                        )
                        break
            if not packet:
                return self._degraded_packet(
                    ticker=ticker,
                    run_id=run_id,
                    error_code="dependency_error",
                    note="Underwriting fallback used: unknown error",
                )

            est = int(packet.get("token_estimate", 0) or 0)
            with budget_lock:
                nonlocal_budget = budget_used + est
                if nonlocal_budget > token_budget:
                    return self._degraded_packet(
                        ticker=ticker,
                        run_id=run_id,
                        error_code="budget_exceeded",
                        note="Token budget exceeded; deterministic packet returned.",
                    )
                # reserve consumed budget
                budget_used = nonlocal_budget
            return packet

        # Run limited-concurrency underwriting for llm rows.
        if llm_rows:
            workers = min(max_concurrency, len(llm_rows))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_process_row, row): row for _, row in llm_rows}
                for future in as_completed(futures):
                    row = futures[future]
                    ticker = str(row.get("ticker", "")).upper()
                    try:
                        packet = future.result()
                    except Exception as exc:  # pragma: no cover
                        packet = self._degraded_packet(
                            ticker=ticker,
                            run_id=run_id,
                            error_code="dependency_error",
                            note=f"Underwriting fallback used: {str(exc)[:200]}",
                        )
                    if packet.get("degraded"):
                        degraded = True
                    packets_by_ticker[ticker] = packet

        # Preserve deterministic order from top_rows.
        packets: List[Dict[str, Any]] = []
        for row in top_rows:
            ticker = str(row.get("ticker", "")).upper()
            if ticker and ticker in packets_by_ticker:
                packets.append(packets_by_ticker[ticker])

        self.db.save_long_horizon_underwriting(run_id=run_id, rows=packets)
        return {
            "run_id": run_id,
            "count": len(packets),
            "items": packets,
            "degraded": degraded,
            "idempotency_key": idempotency_key,
            "model_version": model_version,
            "template_version": template_version,
            "token_budget_per_run": token_budget,
            "token_estimate_used": budget_used,
            "llm_augment_top_k": llm_top_k,
            "llm_call_timeout_seconds": llm_call_timeout,
        }

    def allocate(self, run_id: int, policy_name: str = "default") -> Dict[str, Any]:
        run = self.db.get_screening_run(run_id)
        if not run:
            raise ValueError("run_not_found")
        criteria = self._parse_criteria(run.get("criteria"))
        if criteria.get("strategy") != "long_horizon":
            raise ValueError("not_long_horizon_run")

        details = self.db.get_long_horizon_run_details(run_id=run_id)
        screen_rows = self.db.get_screening_results(run_id) if hasattr(self.db, "get_screening_results") else []
        requested_top_n = int(
            criteria.get("requested_top_n")
            or self.long_cfg.get("default_top_n", 30)
        )
        by_ticker = {str(r.get("ticker", "")).upper(): r for r in (screen_rows or [])}
        # Rebuild from the scored universe when overlay details are a stale
        # or partial persist (run 374 saved 5 allocated names of 120).
        use_full_universe = bool(screen_rows) and (
            not details or len(details) < min(requested_top_n, len(screen_rows))
        )
        rows = []
        if use_full_universe:
            for r in screen_rows:
                merged = dict(r)
                merged["signals"] = self._coerce_signals(merged.get("signals"))
                rows.append(merged)
        else:
            for d in details:
                merged = dict(by_ticker.get(str(d.get("ticker", "")).upper()) or {})
                merged.update(d)
                merged["signals"] = self._coerce_signals(merged.get("signals"))
                rows.append(merged)
        rows = sorted(rows, key=lambda r: float(r.get("composite_score", 0)), reverse=True)
        overlayed, allocation = self._apply_risk_overlay_and_allocate(
            rows, top_n=requested_top_n if use_full_universe else max(len(rows), 1),
            preset=criteria.get("preset"),
        )
        self.db.save_long_horizon_run_details(run_id=run_id, details=overlayed)
        beta_map = {
            str(e.get("ticker", "")).upper(): e.get("beta")
            for e in overlayed
            if str(e.get("ticker", "")).strip()
        }
        portfolio_risk = self.compute_portfolio_risk_summary(
            allocation=allocation, scan_results=screen_rows or overlayed, beta_map=beta_map,
        )
        existing = self.db.get_long_horizon_run_meta(run_id) if hasattr(self.db, "get_long_horizon_run_meta") else {}
        meta = dict(existing.get("meta_json") or {})
        meta["portfolio_risk"] = portfolio_risk
        self.db.save_long_horizon_run_meta(run_id=run_id, meta=meta)
        return {
            "run_id": run_id,
            "policy": policy_name,
            "allocation": allocation,
            "top": overlayed,
            "portfolio_risk": portfolio_risk,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _resolve_universe_inputs(
        self,
        watchlist_id: Optional[int],
        tickers: Optional[List[str]],
        use_expanded_universe: bool = False,
    ) -> Tuple[List[str], Dict[str, Any]]:
        symbols: List[str] = []
        meta: Dict[str, Any] = {"watchlist_id": watchlist_id, "source": "custom"}
        if watchlist_id:
            wl = self.db.get_watchlist(int(watchlist_id))
            if not wl:
                raise ValueError("invalid_universe")
            symbols = [_normalize_symbol(t) for t in str(wl.get("tickers", "")).split(",") if _normalize_symbol(t)]
            meta["source"] = "watchlist"
            meta["watchlist_name"] = wl.get("name")
        elif tickers:
            symbols = [_normalize_symbol(t) for t in tickers if _normalize_symbol(t)]
        elif use_expanded_universe:
            symbols, expanded_meta = self._resolve_expanded_universe()
            meta = {**meta, **expanded_meta}
        symbols = list(dict.fromkeys(symbols))
        pre_cap_symbols = list(symbols)
        symbols = self._sort_for_cap(symbols)
        max_universe = int(self.long_cfg.get("universe_policy", {}).get("max_universe_size", 800))
        capped = symbols[:max_universe]
        dropped = symbols[max_universe:]
        return capped, {
            "input_count": len(pre_cap_symbols),
            "post_sort_count": len(symbols),
            "cap_limit": max_universe,
            "dropped_by_cap_count": len(dropped),
            "dropped_by_cap_sample": dropped[:25],
            **meta,
        }

    def _resolve_expanded_universe(self) -> Tuple[List[str], Dict[str, Any]]:
        cfg = self.long_cfg.get("expanded_universe", {})
        if not bool(cfg.get("enabled", True)):
            return [], {"source": "expanded_universe", "expanded_universe_enabled": False}

        include_sources = {str(s).strip().lower() for s in cfg.get("include_sources", ["built-in", "user", "auto"])}
        exclude_prefixes = [str(p).strip().lower() for p in cfg.get("exclude_watchlist_prefixes", ["Movers:"])]
        max_tickers = max(50, int(cfg.get("max_tickers", 1200)))

        watchlists = self.db.get_watchlists() if hasattr(self.db, "get_watchlists") else []
        raw_symbols: List[str] = []
        source_breakdown: Dict[str, int] = {}
        used_watchlists: List[str] = []

        for wl in watchlists:
            source = str(wl.get("source", "user")).strip().lower()
            name = str(wl.get("name", "")).strip()
            if include_sources and source not in include_sources:
                continue
            if any(name.lower().startswith(prefix) for prefix in exclude_prefixes):
                continue
            tickers_csv = str(wl.get("tickers", "") or "")
            symbols = [_normalize_symbol(t) for t in tickers_csv.replace("\n", ",").split(",")]
            symbols = [s for s in symbols if s]
            if not symbols:
                continue
            used_watchlists.append(name)
            source_breakdown[source] = source_breakdown.get(source, 0) + len(symbols)
            raw_symbols.extend(symbols)

        deduped = list(dict.fromkeys(raw_symbols))
        sorted_symbols = self._sort_for_cap(deduped)
        capped = sorted_symbols[:max_tickers]
        dropped = sorted_symbols[max_tickers:]
        return capped, {
            "source": "expanded_universe",
            "expanded_universe_enabled": True,
            "expanded_universe_watchlists_used": len(used_watchlists),
            "expanded_universe_source_breakdown": source_breakdown,
            "expanded_universe_raw_count": len(raw_symbols),
            "expanded_universe_deduped_count": len(deduped),
            "expanded_universe_sorted_count": len(sorted_symbols),
            "expanded_universe_cap_limit": max_tickers,
            "expanded_universe_dropped_by_cap_count": len(dropped),
            "expanded_universe_dropped_by_cap_sample": dropped[:25],
        }

    def _sort_for_cap(self, symbols: List[str]) -> List[str]:
        """Deterministic cap-sort policy.

        Sort key (per watchlist_universe_restructure plan):
            liquidity_rank ASC NULLS LAST,
            avg_dollar_volume_usd DESC,
            market_cap_tier_weight DESC (mega=5 .. micro=1, unknown=0),
            ticker ASC
        """
        if not symbols:
            return []
        deduped = [str(s).upper() for s in symbols if str(s).strip()]
        deduped = list(dict.fromkeys(deduped))
        metadata_map = self.db.get_ticker_metadata_bulk(deduped) if deduped else {}

        def _sort_key(sym: str) -> Tuple[int, int, float, str]:
            meta = metadata_map.get(sym, {}) or {}
            raw_rank = meta.get("liquidity_rank")
            rank_missing = 1
            rank_val = 10**9
            if raw_rank is not None:
                try:
                    rank_val = int(raw_rank)
                    rank_missing = 0
                except Exception:
                    rank_missing = 1
            avg_dollar_volume_usd = float(meta.get("avg_dollar_volume_usd") or 0.0)
            tier = str(meta.get("market_cap_tier") or "").strip().lower()
            tier_weight = {
                "mega": 5,
                "large": 4,
                "mid": 3,
                "small": 2,
                "micro": 1,
            }.get(tier, 0)
            return (rank_missing, rank_val, -avg_dollar_volume_usd, -tier_weight, sym)

        return sorted(deduped, key=_sort_key)

    def _apply_universe_policy(self, tickers: List[str], policy: Dict[str, Any]) -> Tuple[List[str], List[Dict[str, Any]]]:
        min_price = float(policy.get("min_price", 5.0))
        min_market_cap = float(policy.get("min_market_cap", 2_000_000_000))
        min_avg_volume = float(policy.get("min_avg_volume", 750_000))
        allow_adr = bool(policy.get("allow_adr", False))
        exclude_otc = bool(policy.get("exclude_otc", True))
        exclude_etf = bool(policy.get("exclude_etf", True))
        exclude_leveraged = bool(policy.get("exclude_leveraged", True))
        metadata_workers = max(1, int(policy.get("metadata_workers", 12)))
        metadata_max_age_days = max(1, int(policy.get("metadata_max_age_days", 7)))
        info_workers = max(1, int(policy.get("info_workers", 12)))

        eligible: List[str] = []
        excluded: List[Dict[str, Any]] = []

        metadata_map = (
            resolve_and_cache(
                tickers,
                self.db,
                max_workers=metadata_workers,
                max_age_days=metadata_max_age_days,
            )
            if tickers
            else {}
        )

        # Fast prefilter: if cached market cap is already below floor, skip live info call.
        needs_info: List[str] = []
        for t in tickers:
            meta = metadata_map.get(t, {})
            meta_market_cap = float(meta.get("market_cap") or 0.0)
            if meta_market_cap > 0 and meta_market_cap < min_market_cap:
                excluded.append({"ticker": t, "reason_codes": ["market_cap_below_floor"]})
                continue
            needs_info.append(t)

        info_map: Dict[str, Dict[str, Any]] = {}
        if needs_info:
            workers = min(info_workers, len(needs_info))

            def _fetch_info(sym: str) -> Tuple[str, Dict[str, Any]]:
                try:
                    return sym, (get_ticker_info(sym) or {})
                except Exception:
                    return sym, {}

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_info, sym): sym for sym in needs_info}
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        ticker, info = future.result()
                        info_map[ticker] = info or {}
                    except Exception:
                        info_map[sym] = {}

        for t in needs_info:
            info = info_map.get(t, {})
            meta = metadata_map.get(t, {})
            from tradingagents.screening.ticker_resolver import classify_asset_class

            quote_type = str(info.get("quoteType", "")).lower()
            exchange = str(info.get("exchange", "")).upper()
            asset_class = classify_asset_class(info)
            avg_vol = info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0
            market_cap = info.get("marketCap") or meta.get("market_cap") or 0
            price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose") or 0

            reasons: List[str] = []
            if exclude_etf and (quote_type == "etf" or asset_class == "etf"):
                reasons.append("excluded_etf")
            if exclude_otc and exchange in {"PNK", "OTC", "OTCMKTS"}:
                reasons.append("excluded_otc")
            if (not allow_adr) and asset_class == "adr":
                reasons.append("excluded_adr")
            if exclude_leveraged and _looks_leveraged_or_inverse(info):
                reasons.append("excluded_leveraged")
            if float(price or 0) < min_price:
                reasons.append("price_below_floor")
            if float(market_cap or 0) < min_market_cap:
                reasons.append("market_cap_below_floor")
            if float(avg_vol or 0) < min_avg_volume:
                reasons.append("liquidity_below_floor")

            if reasons:
                excluded.append({"ticker": t, "reason_codes": reasons})
            else:
                eligible.append(t)
            # Persist live cap/class so overlay does not see wiped/stale NULL tiers.
            if hasattr(self.db, "save_ticker_metadata") and (market_cap or info.get("sector") or asset_class):
                from tradingagents.screening.ticker_resolver import classify_market_cap
                try:
                    self.db.save_ticker_metadata(
                        ticker=t,
                        sector=info.get("sector") or meta.get("sector"),
                        market_cap=float(market_cap) if market_cap else None,
                        market_cap_tier=classify_market_cap(market_cap) if market_cap else None,
                        asset_class=asset_class if asset_class and asset_class != "unknown" else None,
                    )
                except Exception:
                    pass
        return eligible, excluded

    def _hydrate_overlay_fundamentals(
        self,
        tickers: List[str],
        metadata_map: Dict[str, Dict[str, Any]],
    ) -> Tuple[set, Dict[str, Optional[float]]]:
        """Fill missing cap/beta from cached ticker info; return ETF set + beta map."""
        from tradingagents.screening.ticker_resolver import classify_asset_class, classify_market_cap

        risk_workers = max(1, int(self.long_cfg.get("execution_profile", {}).get("risk_workers", 8)))
        etf_tickers: set = set()
        beta_lookup: Dict[str, Optional[float]] = {}
        needs_info: List[str] = []
        for sym in tickers:
            meta = metadata_map.get(sym, {}) or {}
            tier = str(meta.get("market_cap_tier") or "").strip().lower()
            if (not tier or tier == "unknown") and not meta.get("market_cap"):
                needs_info.append(sym)
            try:
                if meta.get("beta") is not None:
                    beta_lookup[sym] = float(meta["beta"])
            except (TypeError, ValueError):
                pass

        info_map: Dict[str, Dict[str, Any]] = {}
        if needs_info:
            workers = min(risk_workers, len(needs_info))

            def _fetch_info(sym: str) -> Tuple[str, Dict[str, Any]]:
                try:
                    return sym, (get_ticker_info(sym) or {})
                except Exception:
                    return sym, {}

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_info, t): t for t in needs_info}
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        _, info = future.result()
                    except Exception:
                        info = {}
                    info_map[sym] = info or {}

        for sym in tickers:
            info = info_map.get(sym) or {}
            meta = metadata_map.setdefault(sym, {})
            if classify_asset_class(info) == "etf":
                etf_tickers.add(sym)
            market_cap = info.get("marketCap") or meta.get("market_cap")
            if market_cap and (not meta.get("market_cap_tier") or meta.get("market_cap_tier") == "unknown"):
                meta["market_cap"] = market_cap
                meta["market_cap_tier"] = classify_market_cap(market_cap)
            if info.get("sector") and not meta.get("sector"):
                meta["sector"] = info.get("sector")
            if beta_lookup.get(sym) is None:
                raw_beta = info.get("beta")
                try:
                    if raw_beta is not None:
                        beta_lookup[sym] = float(raw_beta)
                except (TypeError, ValueError):
                    pass
            if hasattr(self.db, "save_ticker_metadata") and (meta.get("market_cap") or info.get("sector")):
                try:
                    self.db.save_ticker_metadata(
                        ticker=sym,
                        sector=meta.get("sector"),
                        market_cap=meta.get("market_cap"),
                        market_cap_tier=meta.get("market_cap_tier"),
                        beta=beta_lookup.get(sym),
                    )
                except Exception:
                    pass

        missing_beta = [s for s in tickers if s not in etf_tickers and beta_lookup.get(s) is None]
        if missing_beta:
            workers = min(risk_workers, len(missing_beta))

            def _fetch_beta(sym: str) -> Tuple[str, Optional[float]]:
                try:
                    return sym, compute_risk_metrics(ticker=sym, period=252).get("beta")
                except Exception:
                    return sym, None

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_beta, t): t for t in missing_beta}
                for future in as_completed(futures):
                    sym = futures[future]
                    try:
                        _, beta_val = future.result()
                    except Exception:
                        beta_val = None
                    beta_lookup[sym] = beta_val
        return etf_tickers, beta_lookup

    def _apply_risk_overlay_and_allocate(
        self,
        rows: List[Dict[str, Any]],
        top_n: int,
        preset: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        rows = rows[: max(1, int(top_n))]
        if not rows:
            return [], {"weights": [], "residual_cash_weight": 1.0, "reason_codes": ["no_rows"]}

        alloc_cfg = self.long_cfg.get("allocation", {})
        max_single = float(alloc_cfg.get("max_single_name_weight", 0.08))
        max_sector = float(alloc_cfg.get("max_sector_weight", 0.25))
        beta_min = float(alloc_cfg.get("beta_min", 0.70))
        beta_max = float(alloc_cfg.get("beta_max", 1.30))
        beta_hard_max = float(alloc_cfg.get("beta_hard_max", 2.00))
        beta_penalty_factor = float(alloc_cfg.get("beta_out_of_band_penalty_factor", 0.75))
        large_cap_min = float(alloc_cfg.get("market_cap_band_min", {}).get("large_or_above", 0.0))
        small_cap_max = float(alloc_cfg.get("market_cap_band_max", {}).get("small_or_below", 0.20))
        allow_relaxations = bool(alloc_cfg.get("allow_relaxations", True))
        relaxation_order = [str(x) for x in alloc_cfg.get("relaxation_order", [])]
        hard_constraints = {str(x) for x in alloc_cfg.get("hard_constraints", [])}

        # deterministic ordering: score desc then ticker asc
        ordered = sorted(rows, key=lambda r: (-float(r.get("composite_score", 0)), str(r.get("ticker", ""))))

        tickers = [str(r.get("ticker", "")).upper() for r in ordered if str(r.get("ticker", "")).strip()]
        from tradingagents.screening.engine import primary_sleeve_present
        from tradingagents.screening.ticker_resolver import classify_market_cap

        # Batch metadata lookup to avoid N+1 DB calls.
        metadata_map = self.db.get_ticker_metadata_bulk(tickers) if tickers else {}
        etf_tickers, beta_lookup = self._hydrate_overlay_fundamentals(tickers, metadata_map)

        working = []
        for r in ordered:
            ticker = str(r.get("ticker", "")).upper()
            meta = metadata_map.get(ticker, {})
            sector = str(meta.get("sector") or "").strip() or "Unknown"
            mcap_tier = str(meta.get("market_cap_tier") or "").strip().lower()
            if not mcap_tier or mcap_tier == "unknown":
                inferred = classify_market_cap(meta.get("market_cap"))
                if inferred and inferred != "unknown":
                    mcap_tier = inferred
            if not mcap_tier:
                mcap_tier = "unknown"
            beta = beta_lookup.get(ticker)
            signals = self._coerce_signals(r.get("signals"))
            sleeve_ok = True
            gate_codes: List[str] = []
            if ticker in etf_tickers:
                sleeve_ok = False
                gate_codes = ["excluded_etf"]
            elif self._implausible_mega_cap(ticker, meta.get("market_cap") or 0, mcap_tier):
                sleeve_ok = False
                gate_codes = ["excluded_data_error"]
            elif beta is not None and abs(float(beta)) > beta_hard_max:
                sleeve_ok = False
                gate_codes = ["excluded_extreme_beta"]
            elif preset and signals and any(
                k in signals for k in ("quality_factor", "valuation_gap", "income_factor")
            ):
                sleeve_ok = primary_sleeve_present(preset, signals)
                if not sleeve_ok:
                    gate_codes = ["sleeve_identity_missing"]
            entry = dict(r)
            entry.update(
                {
                    "ticker": ticker,
                    "base_weight": 0.0,
                    "target_weight": 0.0,
                    "pre_beta_weight": 0.0,
                    "sector": sector,
                    "market_cap_tier": mcap_tier,
                    "beta": beta,
                    "allocation_reason_codes": list(gate_codes),
                    "constraint_hits": [] if sleeve_ok else (
                        ["excluded_etf"] if ticker in etf_tickers
                        else (
                            ["data_error"] if "excluded_data_error" in gate_codes
                            else (["beta_hard_max"] if "excluded_extreme_beta" in gate_codes else ["primary_sleeve"])
                        )
                    ),
                }
            )
            if not sleeve_ok:
                entry["target_weight"] = 0.0
                entry["base_weight"] = 0.0
            working.append(entry)

        _gated = {"sleeve_identity_missing", "excluded_etf", "excluded_data_error", "excluded_extreme_beta"}
        score_sum = sum(
            max(0.0, float(e.get("composite_score", 0)))
            for e in working
            if not _gated.intersection(e.get("allocation_reason_codes", []))
        ) or 1.0
        for e in working:
            if _gated.intersection(e.get("allocation_reason_codes", [])):
                continue
            e["base_weight"] = round(max(0.0, float(e.get("composite_score", 0))) / score_sum, 6)
            e["target_weight"] = e["base_weight"]
            e["pre_beta_weight"] = e["base_weight"]

        # 1) max single-name cap
        for e in working:
            if e["target_weight"] > max_single:
                e["target_weight"] = max_single
                e["allocation_reason_codes"].append("clipped_single_name_cap")
                e["constraint_hits"].append("max_single_name_weight")

        # 2) beta exposure bound + 5) liquidity floor (already filtered in universe policy)
        for e in working:
            beta_val = e.get("beta")
            if beta_val is None:
                continue
            if abs(float(beta_val)) > beta_hard_max:
                e["target_weight"] = 0.0
                e["base_weight"] = 0.0
                e["allocation_reason_codes"].append("excluded_extreme_beta")
                e["constraint_hits"].append("beta_hard_max")
                continue
            if beta_val < beta_min or beta_val > beta_max:
                e["target_weight"] = round(e["target_weight"] * beta_penalty_factor, 6)
                e["allocation_reason_codes"].append("relaxed_beta_bound")
                e["constraint_hits"].append("beta_exposure_bound")

        # 3) sector cap pass
        sector_weights: Dict[str, float] = {}
        for e in working:
            sector_weights[e["sector"]] = sector_weights.get(e["sector"], 0.0) + e["target_weight"]
        for sector in sorted(sector_weights.keys()):
            total = sector_weights[sector]
            if total <= max_sector or total <= 0:
                continue
            factor = max_sector / total
            for e in working:
                if e["sector"] == sector:
                    e["target_weight"] = round(e["target_weight"] * factor, 6)
                    e["allocation_reason_codes"].append("clipped_sector_cap")
                    e["constraint_hits"].append("sector_cap")

        # 4) market-cap band cap for small/micro/unknown
        small_rows = [e for e in working if e["market_cap_tier"] in {"small", "micro"}]
        small_total = sum(e["target_weight"] for e in small_rows)
        if small_total > small_cap_max and small_total > 0:
            factor = small_cap_max / small_total
            for e in small_rows:
                e["target_weight"] = round(e["target_weight"] * factor, 6)
                e["allocation_reason_codes"].append("clipped_market_cap_band")
                e["constraint_hits"].append("market_cap_band_max")

        # 5) market-cap band minimum for large/mega names.
        def _enforce_large_cap_min() -> bool:
            if large_cap_min <= 0:
                return True
            large_rows = [e for e in working if e["market_cap_tier"] in {"large", "mega"}]
            if not large_rows:
                for e in working:
                    e["allocation_reason_codes"].append("infeasible_market_cap_band_min")
                return False
            large_total = sum(e["target_weight"] for e in large_rows)
            if large_total >= large_cap_min:
                return True
            need = large_cap_min - large_total
            donor_rows = [e for e in working if e["market_cap_tier"] not in {"large", "mega"} and e["target_weight"] > 0]
            donor_total = sum(e["target_weight"] for e in donor_rows)
            if donor_total <= 0:
                return False
            shift = min(need, donor_total)
            donor_scale = max(0.0, (donor_total - shift) / donor_total)
            for e in donor_rows:
                e["target_weight"] = round(e["target_weight"] * donor_scale, 6)
                e["allocation_reason_codes"].append("clipped_market_cap_band_min_support")
                e["constraint_hits"].append("market_cap_band_min")
            large_base = sum(e["target_weight"] for e in large_rows)
            if large_base <= 0:
                add_each = shift / max(1, len(large_rows))
                for e in large_rows:
                    e["target_weight"] = round(e["target_weight"] + add_each, 6)
            else:
                for e in large_rows:
                    add = shift * (e["target_weight"] / large_base)
                    e["target_weight"] = round(e["target_weight"] + add, 6)
            for e in large_rows:
                e["allocation_reason_codes"].append("enforced_market_cap_band_min")
                e["constraint_hits"].append("market_cap_band_min")
            return sum(e["target_weight"] for e in large_rows) >= large_cap_min - 1e-6

        large_min_met = _enforce_large_cap_min()
        if not large_min_met and allow_relaxations:
            for step in relaxation_order:
                if large_min_met:
                    break
                if step == "beta_min":
                    for e in working:
                        if e["market_cap_tier"] in {"large", "mega"} and "beta_exposure_bound" in e["constraint_hits"]:
                            e["target_weight"] = max(e["target_weight"], e.get("pre_beta_weight", e["target_weight"]))
                            e["allocation_reason_codes"].append("relaxed_beta_min")
                    large_min_met = _enforce_large_cap_min()
                elif step == "sector_soft_cap" and "sector_cap" not in hard_constraints:
                    soft_sector_cap = max(max_sector * 1.15, large_cap_min)
                    sector_weights_soft: Dict[str, float] = {}
                    for e in working:
                        sector_weights_soft[e["sector"]] = sector_weights_soft.get(e["sector"], 0.0) + e["target_weight"]
                    for sector in sorted(sector_weights_soft.keys()):
                        total = sector_weights_soft[sector]
                        if total <= soft_sector_cap or total <= 0:
                            continue
                        factor = soft_sector_cap / total
                        for e in working:
                            if e["sector"] == sector:
                                e["target_weight"] = round(e["target_weight"] * factor, 6)
                                e["allocation_reason_codes"].append("relaxed_sector_soft_cap")
                    large_min_met = _enforce_large_cap_min()
                elif step == "market_cap_band_min":
                    large_min_met = _enforce_large_cap_min()

        if not large_min_met:
            for e in working:
                e["allocation_reason_codes"].append("market_cap_band_min_unmet")

        # Re-apply hard max-single cap after relaxations, if configured as hard.
        if "max_single_name_weight" in hard_constraints:
            for e in working:
                if e["target_weight"] > max_single:
                    e["target_weight"] = round(max_single, 6)
                    e["allocation_reason_codes"].append("reclipped_hard_single_name_cap")
                    e["constraint_hits"].append("max_single_name_weight")

        # Renormalize if total > 1.0 (never levered)
        total_weight = sum(e["target_weight"] for e in working)
        if total_weight > 1.0 and total_weight > 0:
            scale = 1.0 / total_weight
            for e in working:
                e["target_weight"] = round(e["target_weight"] * scale, 6)
                e["allocation_reason_codes"].append("normalized_total_weight")
            total_weight = 1.0

        residual = round(max(0.0, 1.0 - total_weight), 6)
        if residual > 0:
            for e in working:
                e["allocation_reason_codes"].append("residual_to_cash")

        allocation = {
            "weights": [
                {"ticker": e["ticker"], "weight": e["target_weight"], "sector": e["sector"]}
                for e in working
                if e["target_weight"] > 0
            ],
            "residual_cash_weight": residual,
            "reason_codes": sorted({code for e in working for code in e["allocation_reason_codes"]}),
        }
        return working, allocation

    def compute_portfolio_risk_summary(
        self,
        allocation: Dict[str, Any],
        scan_results: Optional[List[Dict[str, Any]]] = None,
        beta_map: Optional[Dict[str, Optional[float]]] = None,
    ) -> Dict[str, Any]:
        """Compute portfolio-level factor exposures, sector concentration, and weighted beta.

        Args:
            allocation: Output dict from _apply_risk_overlay_and_allocate containing 'weights'.
            scan_results: Optional list of ScreeningResult dicts (asdict) for factor scorecard data.
            beta_map: Optional pre-computed {ticker: beta} map from the allocation pass. When
                provided, avoids a second round of compute_risk_metrics calls (which each hit
                yfinance). Tickers absent from the map fall through to live computation.

        Returns:
            Dict with:
                weighted_beta (float|None), sector_concentration (dict),
                sector_hhi (float), factor_exposures (dict), top_factor (str|None),
                computed_at (str)
        """
        weights = allocation.get("weights", [])
        if not weights:
            return {"weighted_beta": None, "sector_concentration": {}, "sector_hhi": 0.0,
                    "factor_exposures": {}, "top_factor": None, "computed_at": _utc_iso()}

        # Index scan results by ticker for fast lookup
        scorecard_map: Dict[str, Dict[str, float]] = {}
        if scan_results:
            for row in scan_results:
                sc = row.get("factor_scorecard")
                if not (sc and isinstance(sc, dict)):
                    signals = self._coerce_signals(row.get("signals"))
                    raw_sc = signals.get("_factor_scorecard")
                    if isinstance(raw_sc, str):
                        try:
                            raw_sc = json.loads(raw_sc)
                        except (json.JSONDecodeError, TypeError):
                            raw_sc = None
                    sc = raw_sc if isinstance(raw_sc, dict) else None
                if sc and isinstance(sc, dict):
                    scorecard_map[str(row.get("ticker", "")).upper()] = sc

        # ---- Weighted beta ----
        # Use the pre-computed beta_map from the allocation pass to avoid a second
        # round of yfinance calls for the same tickers.
        total_weight = sum(float(w.get("weight", 0) or 0) for w in weights)
        weighted_beta: Optional[float] = None
        if total_weight > 0:
            beta_sum = 0.0
            beta_covered_weight = 0.0
            for w in weights:
                ticker = str(w.get("ticker", "")).upper()
                wt = float(w.get("weight", 0) or 0)
                if wt <= 0:
                    continue
                # Fast path: use the beta already computed during allocation
                b: Optional[float] = None
                if beta_map and ticker in beta_map:
                    b = beta_map[ticker]
                else:
                    try:
                        risk = compute_risk_metrics(ticker=ticker, period=252)
                        b = risk.get("beta")
                    except Exception:
                        pass
                if b is not None:
                    beta_sum += float(b) * wt
                    beta_covered_weight += wt
            if beta_covered_weight > 0:
                weighted_beta = round(beta_sum / beta_covered_weight, 4)

        # ---- Sector concentration ----
        sector_weights_raw: Dict[str, float] = {}
        for w in weights:
            sector = str(w.get("sector") or "Unknown")
            sector_weights_raw[sector] = sector_weights_raw.get(sector, 0.0) + float(w.get("weight", 0) or 0)
        if total_weight > 0:
            sector_pcts = {s: round(v / total_weight, 4) for s, v in sector_weights_raw.items()}
        else:
            sector_pcts = {}
        sector_hhi = round(sum(v ** 2 for v in sector_pcts.values()), 4)

        # ---- Factor exposures (weighted average of per-ticker factor scorecards) ----
        factor_sums: Dict[str, float] = {}
        factor_covered: Dict[str, float] = {}
        for w in weights:
            ticker = str(w.get("ticker", "")).upper()
            wt = float(w.get("weight", 0) or 0)
            sc = scorecard_map.get(ticker)
            if sc and wt > 0:
                for fam, score in sc.items():
                    factor_sums[fam] = factor_sums.get(fam, 0.0) + float(score or 0) * wt
                    factor_covered[fam] = factor_covered.get(fam, 0.0) + wt
        factor_exposures = {
            fam: round(factor_sums[fam] / factor_covered[fam], 2)
            for fam in factor_sums
            if factor_covered.get(fam, 0) > 0
        }
        top_factor = max(factor_exposures, key=lambda k: factor_exposures[k]) if factor_exposures else None

        return {
            "weighted_beta": weighted_beta,
            "sector_concentration": sector_pcts,
            "sector_hhi": sector_hhi,
            "factor_exposures": factor_exposures,
            "top_factor": top_factor,
            "computed_at": _utc_iso(),
        }

    def _build_underwriting_packet(
        self,
        row: Dict[str, Any],
        run_id: int,
        date: Optional[str],
        allow_llm: bool = True,
    ) -> Dict[str, Any]:
        ticker = str(row.get("ticker", "")).upper()
        packet = {
            "run_id": run_id,
            "ticker": ticker,
            "degraded": False,
            "error_code": None,
            "token_estimate": 1200,
            "base_case": f"{ticker} maintains trend durability with moderated volatility.",
            "bull_case": f"{ticker} compounds with revisions/rating support and stable risk profile.",
            "bear_case": f"{ticker} underperforms if revisions fade and valuation rerates lower.",
            "disconfirming_signals": [
                "estimate_momentum_turns_negative",
                "relative_strength_breakdown",
                "beta_out_of_band",
            ],
            "monitoring_checklist": [
                "quarterly_revenue_growth_trend",
                "margin_stability",
                "estimate_revision_breadth",
                "risk_metrics_refresh",
            ],
            "generated_at": _utc_iso(),
        }

        if not allow_llm:
            packet["degraded"] = True
            packet["error_code"] = "disabled"
            packet["token_estimate"] = 0
            return packet

        # Optional augmentation with existing research pipeline; failures degrade gracefully.
        try:
            agent = ResearchAgent(config=self.config, auto_report=False, auto_save=False, debug=False)
            result = agent.analyze(
                ticker=ticker,
                date=date or _utc_now().strftime("%Y-%m-%d"),
                generate_report=False,
                save_to_db=False,
                screening_run_id=run_id,
            )
            packet["base_case"] = (result.state.get("investment_debate_state", {}) or {}).get("judge_decision", packet["base_case"])[:1200]
            packet["bull_case"] = str(result.state.get("bull_research_report", packet["bull_case"]))[:1200]
            packet["bear_case"] = str(result.state.get("bear_research_report", packet["bear_case"]))[:1200]
            packet["token_estimate"] = int((result.state.get("total_tokens") or 0) or 1500)
        except Exception as exc:  # pragma: no cover
            packet["degraded"] = True
            packet["error_code"] = "dependency_error"
            packet["fallback_note"] = f"LLM underwriting fallback: {str(exc)[:180]}"
            packet["token_estimate"] = 0
        return packet

    def _degraded_packet(self, ticker: str, run_id: int, error_code: str, note: str) -> Dict[str, Any]:
        return {
            "run_id": run_id,
            "ticker": ticker,
            "degraded": True,
            "error_code": error_code,
            "token_estimate": 0,
            "base_case": "Deterministic-only mode.",
            "bull_case": "Unavailable in degraded mode.",
            "bear_case": "Unavailable in degraded mode.",
            "disconfirming_signals": [],
            "monitoring_checklist": [],
            "fallback_note": note,
            "generated_at": _utc_iso(),
        }

    @staticmethod
    def _call_with_timeout(fn, timeout_seconds: float):
        """Execute a callable with a hard wall-clock wait budget.

        Uses a daemon thread so request handling can proceed even if an upstream
        dependency hangs; any timed-out work is discarded and replaced with a
        deterministic degraded packet.
        """
        done = threading.Event()
        holder: Dict[str, Any] = {}

        def _runner():
            try:
                holder["value"] = fn()
            except Exception as exc:  # pragma: no cover - passthrough
                holder["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()
        finished = done.wait(max(1.0, float(timeout_seconds)))
        if not finished:
            raise TimeoutError("underwrite timeout")
        if "error" in holder:
            raise holder["error"]
        return holder.get("value")

    def _regime_snapshot(self) -> Dict[str, Any]:
        try:
            from tradingagents.dataflows.index_regime import index_regime_storage_fields, normalize_index_regime

            macro_cfg = self.config.get("screening", {}).get("regime_adjustments", {})
            macro = get_macro_snapshot() or {}
            sector = compute_sector_momentum() or {}
            index_fields = normalize_index_regime(macro)
            return {
                "regime_adjustment": macro_cfg,
                **index_regime_storage_fields(macro),
                "market_regime": index_fields.get("market_regime"),
                "vix_proxy": macro.get("vix_proxy"),
                "yield_curve_slope": macro.get("yield_curve_slope"),
                "credit_stress": macro.get("credit_stress"),
                "sector_breadth": macro.get("sector_breadth"),
                "sector_momentum_top3": (sector.get("top3") or [])[:3],
            }
        except Exception:
            return {}

    @staticmethod
    def sort_overlay_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Allocated sleeve first (weight desc), then remaining candidates by score."""
        return sorted(
            rows,
            key=lambda r: (
                0 if float(r.get("target_weight") or 0) > 0 else 1,
                -float(r.get("target_weight") or 0),
                -float(r.get("composite_score") or 0),
                str(r.get("ticker") or ""),
            ),
        )

    @staticmethod
    def allocation_from_overlay(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        weights = [
            {
                "ticker": str(r.get("ticker", "")).upper(),
                "weight": float(r.get("target_weight") or 0),
                "sector": r.get("sector") or "Unknown",
            }
            for r in rows
            if float(r.get("target_weight") or 0) > 0
        ]
        invested = sum(w["weight"] for w in weights)
        return {
            "weights": weights,
            "residual_cash_weight": round(max(0.0, 1.0 - invested), 6),
            "reason_codes": sorted({
                code
                for r in rows
                for code in (r.get("allocation_reason_codes") or [])
            }),
        }

    @staticmethod
    def _implausible_mega_cap(ticker: str, market_cap: Any, tier: str) -> bool:
        """True when Yahoo mapped a mega-cap onto a ticker that is not one."""
        from tradingagents.screening.discovery import is_implausible_mega_identity

        return is_implausible_mega_identity(ticker, market_cap, tier)

    @staticmethod
    def _coerce_signals(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _parse_criteria(criteria_raw: Any) -> Dict[str, Any]:
        if isinstance(criteria_raw, dict):
            return criteria_raw
        if isinstance(criteria_raw, str) and criteria_raw:
            try:
                return json.loads(criteria_raw)
            except Exception:
                return {}
        return {}

