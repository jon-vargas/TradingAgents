"""
TradingAgents Research Module

High-level API for running analyses with automatic reporting and database storage.
Provides a simple, professional-grade research workflow.

Usage:
    from tradingagents.research import ResearchAgent
    
    agent = ResearchAgent()
    result = agent.analyze("NVDA", "2024-11-10")
    
    # Access reports
    print(result.decision)
    print(result.pdf_path)
    print(result.html_path)
    
    # Query historical analyses
    history = agent.get_history("NVDA")
"""

import logging
import os
import time
import json
from datetime import datetime
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

logger = logging.getLogger("tradingagents.research")

from .graph.trading_graph import TradingAgentsGraph
from .default_config import DEFAULT_CONFIG
from .reporting import (
    generate_report,
    ResearchDatabase,
    get_db,
    Analysis,
    create_analysis_from_state,
)
from .utils.config_validation import validate_config


def _persist_catalyst_events(
    ticker: str,
    analysis_date: str,
    state: Dict[str, Any],
    db,
) -> None:
    """Parse catalyst_pipeline text + earnings profile and upsert to event_calendar.

    Called non-fatally after analysis save; failures are silently logged.
    Parses simple YYYY-MM-DD date patterns from the catalyst text so we can
    surface them through get_upcoming_events without a full NLP pipeline.
    """
    import re
    from datetime import datetime as _dt

    today = _dt.now()

    # 1) Earnings profile from state (already structured)
    try:
        from tradingagents.dataflows.yfinance_extended import get_earnings_profile
        ep = get_earnings_profile(ticker)
        ep_date = ep.get("next_earnings_date")
        ep_days = ep.get("days_until_earnings")
        if ep_date and ep_days is not None:
            db.upsert_event_calendar(
                ticker=ticker,
                event_type="earnings",
                event_date=ep_date,
                days_to_event=int(ep_days),
                description=f"Next earnings date ({ticker})",
            )
    except Exception:
        pass

    # 2) Catalyst pipeline text — extract any ISO dates as unstructured catalysts
    pipeline_text = (state.get("catalyst_pipeline") or "").strip()
    if not pipeline_text:
        return

    date_pattern = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    for match in date_pattern.finditer(pipeline_text):
        raw_date = match.group(1)
        try:
            event_dt = _dt.strptime(raw_date, "%Y-%m-%d")
            days_away = (event_dt - today).days
            if -30 <= days_away <= 365:  # only near-term events
                # Extract surrounding context (up to 80 chars)
                start = max(0, match.start() - 40)
                end = min(len(pipeline_text), match.end() + 40)
                context = pipeline_text[start:end].replace("\n", " ").strip()
                db.upsert_event_calendar(
                    ticker=ticker,
                    event_type="catalyst",
                    event_date=raw_date,
                    days_to_event=int(days_away),
                    description=context[:200],
                    metadata={"source": "catalyst_pipeline", "analysis_date": analysis_date},
                )
        except Exception:
            pass


@dataclass
class ResearchResult:
    """Result of a research analysis."""
    ticker: str
    analysis_date: str
    decision: str
    state: Dict[str, Any]
    
    # Generated files
    pdf_path: Optional[str] = None
    html_path: Optional[str] = None
    
    # Database record
    analysis_id: Optional[int] = None
    
    # Timing
    duration_seconds: float = 0.0
    
    # Quick access to reports
    @property
    def market_report(self) -> str:
        return self.state.get("market_report", "")
    
    @property
    def fundamentals_report(self) -> str:
        return self.state.get("fundamentals_report", "")
    
    @property
    def news_report(self) -> str:
        return self.state.get("news_report", "")
    
    @property
    def final_decision_text(self) -> str:
        return self.state.get("final_trade_decision", "")
    
    @property
    def trading_plan(self) -> str:
        return self.state.get("trader_investment_plan", "")


class ResearchAgent:
    """
    High-level research agent with automatic reporting and database storage.
    
    Wraps TradingAgentsGraph with convenient features:
    - Automatic PDF/HTML report generation
    - SQLite database storage for all analyses
    - Historical query capabilities
    - Simple, clean API
    """
    
    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        selected_analysts: Optional[List[str]] = None,
        output_dir: str = "./research_output",
        db_path: str = "./research.db",
        auto_report: bool = True,
        auto_save: bool = True,
        debug: bool = False,
    ):
        """Initialize the research agent.
        
        Args:
            config: Custom configuration (defaults to DEFAULT_CONFIG)
            selected_analysts: List of analysts to use (default: all)
            output_dir: Directory for reports (default: ./research_output)
            db_path: Path to SQLite database (default: ./research.db)
            auto_report: Automatically generate PDF/HTML reports
            auto_save: Automatically save to database
            debug: Enable debug output
        """
        self.config = config or DEFAULT_CONFIG.copy()
        validation_errors = validate_config(self.config)
        if validation_errors:
            raise ValueError(f"Invalid config: {', '.join(validation_errors)}")
        self.selected_analysts = selected_analysts
        self.output_dir = output_dir
        self.auto_report = auto_report
        self.auto_save = auto_save
        self.debug = debug
        
        # Initialize database
        self.db = get_db(db_path)
        
        # Initialize trading agents graph
        self._init_graph()
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
    
    def _init_graph(self):
        """Initialize or reinitialize the trading agents graph."""
        if self.selected_analysts:
            self.graph = TradingAgentsGraph(
                selected_analysts=self.selected_analysts,
                debug=self.debug,
                config=self.config,
            )
        else:
            self.graph = TradingAgentsGraph(
                debug=self.debug,
                config=self.config,
            )
    
    def analyze(
        self,
        ticker: str,
        date: str,
        generate_report: bool = None,
        save_to_db: bool = None,
        investment_profile: dict = None,
        investment_profile_key: Optional[str] = None,
        investment_profile_source: Optional[str] = None,
        watchlist_id: Optional[int] = None,
        screening_run_id: Optional[int] = None,
        screening_context: Optional[dict] = None,
    ) -> ResearchResult:
        """Run a complete analysis on a ticker.
        
        Args:
            ticker: Stock ticker symbol (e.g., "NVDA", "AAPL")
            date: Analysis date in YYYY-MM-DD format
            generate_report: Override auto_report setting
            save_to_db: Override auto_save setting
            investment_profile: Investment profile dict with per-agent directives
            investment_profile_key: Explicit configured profile key, if selected
            investment_profile_source: Optional provenance for a pre-resolved profile
            watchlist_id: Optional watchlist used to resolve Auto profiles
            
        Returns:
            ResearchResult with decision, reports, and paths
        """
        ticker = ticker.upper()
        
        # Determine settings
        should_report = generate_report if generate_report is not None else self.auto_report
        should_save = save_to_db if save_to_db is not None else self.auto_save

        from .utils.investment_profile_resolution import resolve_investment_profile_for_ticker

        resolved_key, resolved_profile, resolved_from = resolve_investment_profile_for_ticker(
            ticker,
            explicit_key=investment_profile_key,
            explicit_profile=investment_profile,
            watchlist_id=watchlist_id,
            db=self.db,
            config=self.config,
        )
        if investment_profile_source and resolved_profile:
            # A caller that already resolved a key (for example, a batch
            # explicit selection) can preserve its provenance.
            resolved_profile["resolved_from"] = investment_profile_source
            resolved_from = investment_profile_source

        profile_name = (investment_profile or {}).get("display_name", "Default")
        profile_name = (resolved_profile or {}).get("display_name", "Generic / Default")
        logger.info("=" * 60)
        logger.info(
            "ANALYZING: %s | Date: %s | Profile: %s (%s)",
            ticker, date, profile_name, resolved_from,
        )
        logger.info("=" * 60)
        
        # Run analysis with timing
        start_time = time.time()
        state, decision = self.graph.propagate(
            ticker, date,
            investment_profile=resolved_profile,
            screening_context=screening_context,
        )
        state["investment_profile_key"] = resolved_key
        state["investment_profile_resolved_from"] = resolved_from
        duration = time.time() - start_time
        
        logger.info("Analysis complete in %.1fs", duration)
        logger.info("Decision: %s", decision)
        
        # Create result
        result = ResearchResult(
            ticker=ticker,
            analysis_date=date,
            decision=decision,
            state=state,
            duration_seconds=duration,
        )
        
        # Generate reports
        if should_report:
            logger.info("Generating reports...")
            try:
                from .reporting import generate_report as gen_report
                report_paths = gen_report(
                    state=state,
                    ticker=ticker,
                    analysis_date=date,
                    decision=decision,
                    config=self.config,
                    duration_seconds=duration,
                    output_dir=self.output_dir,
                    format="both",
                )
                result.html_path = report_paths.get("html")
                result.pdf_path = report_paths.get("pdf")
            except Exception as e:
                logger.warning("Report generation failed: %s", e)
        
        # Save to database
        if should_save:
            logger.info("Saving to database...")
            try:
                analysis = create_analysis_from_state(
                    state=state,
                    ticker=ticker,
                    analysis_date=date,
                    decision=decision,
                    config=self.config,
                    duration_seconds=duration,
                    screening_run_id=screening_run_id,
                    screening_context=screening_context or state.get("screening_context"),
                )
                result.analysis_id = self.db.save_analysis(analysis)
                # Persist catalyst_pipeline events into event_calendar (Plan B P3/P4)
                try:
                    _persist_catalyst_events(ticker, date, state, self.db)
                except Exception as _cpe:
                    logger.debug("catalyst event calendar persist skipped: %s", _cpe)
                # Persist per-model usage breakdown when available. This is
                # optional and non-fatal; summary totals already live on the
                # analyses row.
                usage_summary = state.get("llm_usage_summary")
                if result.analysis_id and isinstance(usage_summary, dict):
                    try:
                        self.db.save_analysis_llm_usage(result.analysis_id, usage_summary)
                        self.db.save_analysis_llm_telemetry(result.analysis_id, usage_summary)
                    except Exception as usage_err:
                        logger.debug("LLM usage breakdown save skipped: %s", usage_err)
                logger.info("Saved with ID: %s", result.analysis_id)
            except Exception as e:
                logger.error(
                    "Database save failed for %s (%s): %s",
                    ticker,
                    date,
                    e,
                    exc_info=True,
                )
                raise RuntimeError(f"Database save failed for {ticker}: {e}") from e
        
        logger.info("=" * 60)
        logger.info("RESULT: %s -> %s", ticker, decision)
        logger.info("=" * 60)
        
        return result
    
    def batch_analyze(
        self,
        tickers: List[str],
        date: str,
        delay_seconds: int = 60,
        save_summary: bool = True,
        summary_path: Optional[str] = None,
        summary_html_path: Optional[str] = None,
        summary_snapshot_filter: Optional[str] = None,
        max_retries: int = 2,
        investment_profile: dict = None,
        investment_profile_key: Optional[str] = None,
        investment_profile_source: Optional[str] = None,
        screening_run_id: Optional[int] = None,
        screening_context: Optional[dict] = None,
    ) -> List[ResearchResult]:
        """Analyze multiple tickers with rate limit protection.
        
        Args:
            tickers: List of ticker symbols
            date: Analysis date
            delay_seconds: Delay between analyses (min 60s recommended for rate limits)
            max_retries: Number of retries on rate limit errors
            
        Returns:
            List of ResearchResult objects
            
        Rate Limit Notes:
            - Finnhub: 60 req/min - delay of 60s+ ensures reset between analyses
            - Alpha Vantage: 25 req/day (free) - use yfinance for batch safety
            - Perplexity: ~100 req/month - Deep mode only
        """
        results = []
        total = len(tickers)
        
        # Ensure minimum safe delay
        effective_delay = max(delay_seconds, 60)
        if delay_seconds < 60:
            logger.warning("Delay increased from %ds to %ds for rate limit safety", delay_seconds, effective_delay)
        
        logger.info("=" * 60)
        logger.info("BATCH ANALYSIS: %d tickers | Date: %s", total, date)
        logger.info("Delay: %ds | Retries: %d", effective_delay, max_retries)
        logger.info("=" * 60)
        
        for i, ticker in enumerate(tickers, 1):
            logger.info("[%d/%d] Analyzing %s...", i, total, ticker)
            
            # Retry logic for rate limit errors
            last_error = None
            for attempt in range(max_retries + 1):
                try:
                    result = self.analyze(
                        ticker,
                        date,
                        investment_profile=investment_profile,
                        investment_profile_key=investment_profile_key,
                        investment_profile_source=investment_profile_source,
                        screening_run_id=screening_run_id,
                        screening_context=(
                            screening_context.get(ticker.upper())
                            if isinstance(screening_context, dict)
                            else screening_context
                        ),
                    )
                    results.append(result)
                    break  # Success, exit retry loop
                except Exception as e:
                    error_str = str(e).lower()
                    is_rate_limit = "rate" in error_str or "limit" in error_str or "429" in error_str
                    
                    if is_rate_limit and attempt < max_retries:
                        # Rate limit hit - wait extra time and retry
                        backoff = (attempt + 1) * 30  # 30s, 60s, ...
                        logger.warning("Rate limit hit, waiting %ds before retry %d/%d...", backoff, attempt + 1, max_retries)
                        time.sleep(backoff)
                        last_error = e
                    else:
                        # Non-rate-limit error or out of retries
                        logger.error("Error analyzing %s: %s", ticker, e)
                        results.append(ResearchResult(
                            ticker=ticker,
                            analysis_date=date,
                            decision=f"ERROR: {e}",
                            state={},
                        ))
                        break
            else:
                # All retries exhausted
                logger.error("All retries exhausted for %s: %s", ticker, last_error)
                results.append(ResearchResult(
                    ticker=ticker,
                    analysis_date=date,
                    decision=f"ERROR: Rate limit - {last_error}",
                    state={},
                ))
            
            # Delay between analyses (except for last one)
            if i < total and effective_delay > 0:
                logger.info("Waiting %ds before next analysis...", effective_delay)
                time.sleep(effective_delay)
        
        # Print summary
        logger.info("=" * 60)
        logger.info("BATCH COMPLETE: %d tickers analyzed", total)
        logger.info("=" * 60)
        
        buy_count = sum(1 for r in results if "BUY" in r.decision.upper())
        sell_count = sum(1 for r in results if "SELL" in r.decision.upper())
        hold_count = sum(1 for r in results if "HOLD" in r.decision.upper())
        error_count = sum(1 for r in results if "ERROR" in r.decision.upper())
        sec_count = sum(1 for r in results if r.state.get("sec_filings_snapshot"))
        transcript_count = sum(1 for r in results if r.state.get("earnings_transcript_snapshot"))
        avg_duration = sum(r.duration_seconds for r in results) / len(results) if results else 0.0
        
        logger.info("BUY:  %d", buy_count)
        logger.info("SELL: %d", sell_count)
        logger.info("HOLD: %d", hold_count)
        if error_count:
            logger.info("ERRORS: %d", error_count)
        logger.info("SEC snapshots: %d", sec_count)
        logger.info("Transcript snapshots: %d", transcript_count)
        logger.info("Avg duration: %.1fs", avg_duration)
        
        if summary_snapshot_filter and summary_snapshot_filter.lower() not in {"all", "any", "sec", "transcript", "none", "multi"}:
            raise ValueError("summary_snapshot_filter must be one of: all, any, sec, transcript, none, multi")

        if save_summary:
            filtered_results = self._filter_batch_results(results, summary_snapshot_filter)
            self._write_batch_summary(
                filtered_results,
                date,
                summary_path,
                summary_snapshot_filter=summary_snapshot_filter,
                batch_total=total,
            )
            self._write_batch_summary_only(
                filtered_results,
                date,
                summary_snapshot_filter=summary_snapshot_filter,
                batch_total=total,
            )
            self._write_batch_metadata_only(
                filtered_results,
                date,
                summary_path=os.path.join(self.output_dir, "summaries", f"batch_summary_meta_{date}.csv"),
                summary_snapshot_filter=summary_snapshot_filter,
                batch_total=total,
            )
            self._write_batch_summary_html(
                filtered_results,
                date,
                summary_html_path,
                summary_snapshot_filter=summary_snapshot_filter,
                batch_total=total,
            )

        return results

    @staticmethod
    def _filter_batch_results(
        results: List[ResearchResult],
        snapshot_filter: Optional[str],
    ) -> List[ResearchResult]:
        if not snapshot_filter:
            return results

        filter_key = snapshot_filter.lower()
        if filter_key in {"all", "any"}:
            return results
        if filter_key == "sec":
            return [r for r in results if r.state.get("sec_filings_snapshot")]
        if filter_key == "transcript":
            return [r for r in results if r.state.get("earnings_transcript_snapshot")]
        if filter_key == "multi":
            return [
                r
                for r in results
                if r.state.get("sec_filings_snapshot") and r.state.get("earnings_transcript_snapshot")
            ]
        if filter_key == "none":
            return [
                r
                for r in results
                if not r.state.get("sec_filings_snapshot") and not r.state.get("earnings_transcript_snapshot")
            ]
        return results

    @staticmethod
    def _percentile(values: List[float], percentile: float) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        if len(sorted_vals) == 1:
            return float(sorted_vals[0])
        rank = (percentile / 100.0) * (len(sorted_vals) - 1)
        low = int(rank)
        high = min(low + 1, len(sorted_vals) - 1)
        fraction = rank - low
        return (sorted_vals[low] * (1 - fraction)) + (sorted_vals[high] * fraction)

    @staticmethod
    def _extract_themes(text: str) -> List[str]:
        if not text:
            return []
        corpus = text.lower()
        themes = {
            "AI/ML": ["ai", "artificial intelligence", "machine learning", "model", "automation"],
            "Earnings": ["earnings", "guidance", "eps", "revenue", "margin"],
            "Macro/Fed": ["fed", "rates", "inflation", "macro", "recession"],
            "Regulatory": ["regulat", "antitrust", "sec", "doj", "policy"],
            "Product/Launch": ["product", "launch", "release", "roadmap", "platform"],
            "Competition": ["competition", "competitor", "rival", "market share"],
            "Supply Chain": ["supply", "manufactur", "logistics", "inventory"],
            "M&A": ["acquisition", "merger", "buyout", "takeover"],
        }
        hits: List[str] = []
        for label, keywords in themes.items():
            if any(keyword in corpus for keyword in keywords):
                hits.append(label)
        return hits

    @staticmethod
    def _themes_for_result(result: "ResearchResult") -> str:
        texts = [
            result.state.get("market_report", ""),
            result.state.get("fundamentals_report", ""),
            result.state.get("news_report", ""),
            result.state.get("sentiment_report", ""),
            result.state.get("sec_filings_snapshot", ""),
            result.state.get("earnings_transcript_snapshot", ""),
        ]
        combined = "\n".join(t for t in texts if t)
        themes = ResearchAgent._extract_themes(combined)
        return ", ".join(themes) if themes else "None"

    @staticmethod
    def _top_tickers_by_metric(results: List["ResearchResult"], metric_key: str, limit: int = 3) -> str:
        scored = []
        for result in results:
            value = result.state.get(metric_key, 0) or 0
            scored.append((value, result.ticker))
        scored.sort(key=lambda item: item[0], reverse=True)
        return ", ".join(f"{ticker} ({value:.1f})" for value, ticker in scored[:limit])

    @staticmethod
    def _top_ticker_rows(results: List["ResearchResult"], metric_key: str, limit: int = 5) -> List[tuple]:
        scored = []
        for result in results:
            value = result.state.get(metric_key, 0) or 0
            scored.append((value, result.ticker))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:limit]

    @staticmethod
    def _top_tickers_by_warnings(results: List["ResearchResult"], limit: int = 3) -> str:
        scored = []
        for result in results:
            count = len(result.state.get("report_warnings", []) or [])
            scored.append((count, result.ticker))
        scored.sort(key=lambda item: item[0], reverse=True)
        return ", ".join(f"{ticker} ({count})" for count, ticker in scored[:limit])

    @staticmethod
    def _top_warning_rows(results: List["ResearchResult"], limit: int = 5) -> List[tuple]:
        scored = []
        for result in results:
            count = len(result.state.get("report_warnings", []) or [])
            scored.append((count, result.ticker))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[:limit]

    def _write_watchlist_brief(
        self,
        results: List["ResearchResult"],
        date: str,
        output_dir: str,
        filter_label: str,
        batch_total: int,
        summary_total: int,
        name_suffix: str,
    ) -> str:
        os.makedirs(output_dir, exist_ok=True)
        top_confidence = ResearchAgent._top_tickers_by_metric(results, "confidence")
        top_quality = ResearchAgent._top_tickers_by_metric(results, "data_quality_score")
        top_warnings = ResearchAgent._top_tickers_by_warnings(results)
        theme_counts: Dict[str, int] = {}
        for result in results:
            for theme in ResearchAgent._extract_themes(
                "\n".join(
                    [
                        result.state.get("market_report", ""),
                        result.state.get("fundamentals_report", ""),
                        result.state.get("news_report", ""),
                        result.state.get("sentiment_report", ""),
                    ]
                )
            ):
                theme_counts[theme] = theme_counts.get(theme, 0) + 1
        top_themes = ", ".join(
            f"{theme} ({count})"
            for theme, count in sorted(theme_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        ) or "None"
        buy_count = sum(1 for r in results if "BUY" in r.decision.upper())
        sell_count = sum(1 for r in results if "SELL" in r.decision.upper())
        hold_count = sum(1 for r in results if "HOLD" in r.decision.upper())
        error_count = sum(1 for r in results if "ERROR" in r.decision.upper())
        sec_count = sum(1 for r in results if r.state.get("sec_filings_snapshot"))
        transcript_count = sum(1 for r in results if r.state.get("earnings_transcript_snapshot"))

        brief = "\n".join(
            [
                f"# Watchlist Brief - {date}",
                "",
                f"- Filter: {filter_label}",
                f"- Total: {summary_total} (batch {batch_total})",
                f"- Decisions: BUY {buy_count} | SELL {sell_count} | HOLD {hold_count} | ERR {error_count}",
                f"- Snapshot coverage: SEC {sec_count} | Transcript {transcript_count}",
                f"- Top themes: {top_themes}",
                f"- Top confidence: {top_confidence or 'None'}",
                f"- Top quality: {top_quality or 'None'}",
                f"- Most warnings: {top_warnings or 'None'}",
            ]
        )

        filename = f"batch_watchlist_brief_{name_suffix}.md"
        path = os.path.join(output_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(brief)
        return path

    def _write_watchlist_brief_json(
        self,
        results: List["ResearchResult"],
        date: str,
        output_dir: str,
        filter_label: str,
        batch_total: int,
        summary_total: int,
        name_suffix: str,
    ) -> str:
        os.makedirs(output_dir, exist_ok=True)
        theme_counts: Dict[str, int] = {}
        for result in results:
            for theme in ResearchAgent._extract_themes(
                "\n".join(
                    [
                        result.state.get("market_report", ""),
                        result.state.get("fundamentals_report", ""),
                        result.state.get("news_report", ""),
                        result.state.get("sentiment_report", ""),
                    ]
                )
            ):
                theme_counts[theme] = theme_counts.get(theme, 0) + 1
        top_themes = [
            {"theme": theme, "count": count}
            for theme, count in sorted(theme_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        ]
        data = {
            "date": date,
            "filter": filter_label,
            "summary_total": summary_total,
            "batch_total": batch_total,
            "decisions": {
                "buy": sum(1 for r in results if "BUY" in r.decision.upper()),
                "sell": sum(1 for r in results if "SELL" in r.decision.upper()),
                "hold": sum(1 for r in results if "HOLD" in r.decision.upper()),
                "errors": sum(1 for r in results if "ERROR" in r.decision.upper()),
            },
            "snapshots": {
                "sec": sum(1 for r in results if r.state.get("sec_filings_snapshot")),
                "transcript": sum(1 for r in results if r.state.get("earnings_transcript_snapshot")),
            },
            "top_themes": top_themes,
            "top_confidence": ResearchAgent._top_ticker_rows(results, "confidence"),
            "top_quality": ResearchAgent._top_ticker_rows(results, "data_quality_score"),
            "top_warnings": ResearchAgent._top_warning_rows(results),
        }
        filename = f"batch_watchlist_brief_{name_suffix}.json"
        path = os.path.join(output_dir, filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        return path

    def _write_watchlist_brief_csv(
        self,
        results: List["ResearchResult"],
        date: str,
        output_dir: str,
        filter_label: str,
        batch_total: int,
        summary_total: int,
        name_suffix: str,
    ) -> str:
        import csv

        os.makedirs(output_dir, exist_ok=True)
        theme_counts: Dict[str, int] = {}
        for result in results:
            for theme in ResearchAgent._extract_themes(
                "\n".join(
                    [
                        result.state.get("market_report", ""),
                        result.state.get("fundamentals_report", ""),
                        result.state.get("news_report", ""),
                        result.state.get("sentiment_report", ""),
                    ]
                )
            ):
                theme_counts[theme] = theme_counts.get(theme, 0) + 1
        top_themes = sorted(theme_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        top_confidence = ResearchAgent._top_ticker_rows(results, "confidence")
        top_quality = ResearchAgent._top_ticker_rows(results, "data_quality_score")
        top_warnings = ResearchAgent._top_warning_rows(results)

        def _format_rank(row: Optional[tuple], is_percent: bool) -> str:
            if not row:
                return "-"
            value, ticker = row
            if is_percent:
                return f"{ticker} ({value:.1f})"
            return f"{ticker} ({int(value)})"

        max_len = max(len(top_confidence), len(top_quality), len(top_warnings), 1)
        filename = f"batch_watchlist_brief_{name_suffix}.csv"
        path = os.path.join(output_dir, filename)
        with open(path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["key", "value"])
            writer.writerow(["date", date])
            writer.writerow(["filter", filter_label])
            writer.writerow(["summary_total", summary_total])
            writer.writerow(["batch_total", batch_total])
            writer.writerow(["buy", sum(1 for r in results if "BUY" in r.decision.upper())])
            writer.writerow(["sell", sum(1 for r in results if "SELL" in r.decision.upper())])
            writer.writerow(["hold", sum(1 for r in results if "HOLD" in r.decision.upper())])
            writer.writerow(["errors", sum(1 for r in results if "ERROR" in r.decision.upper())])
            writer.writerow(["sec_snapshots", sum(1 for r in results if r.state.get("sec_filings_snapshot"))])
            writer.writerow(
                ["transcript_snapshots", sum(1 for r in results if r.state.get("earnings_transcript_snapshot"))]
            )
            writer.writerow(
                ["top_themes", ", ".join(f"{theme} ({count})" for theme, count in top_themes) or "None"]
            )
            writer.writerow(
                [
                    "top_confidence",
                    ", ".join(_format_rank(row, True) for row in top_confidence) or "None",
                ]
            )
            writer.writerow(
                ["top_quality", ", ".join(_format_rank(row, True) for row in top_quality) or "None"]
            )
            writer.writerow(
                ["top_warnings", ", ".join(_format_rank(row, False) for row in top_warnings) or "None"]
            )
            writer.writerow([])
            writer.writerow(["theme", "count"])
            for theme, count in top_themes:
                writer.writerow([theme, count])
            writer.writerow([])
            writer.writerow(["rank", "top_confidence", "top_quality", "top_warnings"])
            for idx in range(max_len):
                conf = top_confidence[idx] if idx < len(top_confidence) else None
                qual = top_quality[idx] if idx < len(top_quality) else None
                warn = top_warnings[idx] if idx < len(top_warnings) else None
                writer.writerow(
                    [
                        idx + 1,
                        _format_rank(conf, True),
                        _format_rank(qual, True),
                        _format_rank(warn, False),
                    ]
                )
        return path

    def _write_batch_summary(
        self,
        results: List[ResearchResult],
        date: str,
        summary_path: Optional[str] = None,
        summary_snapshot_filter: Optional[str] = None,
        batch_total: Optional[int] = None,
    ) -> str:
        """Write a CSV summary for batch runs."""
        import csv

        if summary_path is None:
            summaries_dir = os.path.join(self.output_dir, "summaries")
            os.makedirs(summaries_dir, exist_ok=True)
            summary_path = os.path.join(summaries_dir, f"batch_summary_{date}.csv")

        os.makedirs(os.path.dirname(summary_path), exist_ok=True)
        snapshots_dir = os.path.join(self.output_dir, "snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)
        snapshot_links = ResearchAgent._write_snapshot_files(self, results, snapshots_dir, date)

        summary_total = len(results)
        batch_total = batch_total if batch_total is not None else summary_total
        filter_label = (summary_snapshot_filter or "all").lower()
        base_name = os.path.splitext(os.path.basename(summary_path))[0]
        name_suffix = base_name[len("batch_summary_only_"):] if base_name.startswith("batch_summary_only_") else date
        brief_path = ResearchAgent._write_watchlist_brief(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_name = os.path.basename(brief_path)
        brief_json_path = ResearchAgent._write_watchlist_brief_json(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_json_name = os.path.basename(brief_json_path)
        brief_csv_path = ResearchAgent._write_watchlist_brief_csv(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_csv_name = os.path.basename(brief_csv_path)
        brief_json_path = ResearchAgent._write_watchlist_brief_json(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_json_name = os.path.basename(brief_json_path)
        brief_csv_path = ResearchAgent._write_watchlist_brief_csv(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_csv_name = os.path.basename(brief_csv_path)
        brief_csv_path = ResearchAgent._write_watchlist_brief_csv(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_csv_name = os.path.basename(brief_csv_path)
        base_name = os.path.splitext(os.path.basename(summary_path))[0]
        name_suffix = base_name[len("batch_summary_"):] if base_name.startswith("batch_summary_") else date
        brief_path = ResearchAgent._write_watchlist_brief(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_name = os.path.basename(brief_path)
        brief_json_path = ResearchAgent._write_watchlist_brief_json(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_json_name = os.path.basename(brief_json_path)
        buy_count = sum(1 for r in results if "BUY" in r.decision.upper())
        sell_count = sum(1 for r in results if "SELL" in r.decision.upper())
        hold_count = sum(1 for r in results if "HOLD" in r.decision.upper())
        error_count = sum(1 for r in results if "ERROR" in r.decision.upper())
        sec_count = sum(1 for r in results if r.state.get("sec_filings_snapshot"))
        transcript_count = sum(1 for r in results if r.state.get("earnings_transcript_snapshot"))
        avg_duration = sum(r.duration_seconds for r in results) / summary_total if summary_total else 0.0
        durations = [r.duration_seconds for r in results if r.duration_seconds is not None]
        duration_p50 = ResearchAgent._percentile(durations, 50.0)
        duration_p75 = ResearchAgent._percentile(durations, 75.0)
        duration_p90 = ResearchAgent._percentile(durations, 90.0)
        avg_confidence = (
            sum((r.state.get("confidence", 0) or 0) for r in results) / summary_total
            if summary_total
            else 0.0
        )
        avg_quality = (
            sum((r.state.get("data_quality_score", 0) or 0) for r in results) / summary_total
            if summary_total
            else 0.0
        )
        qa_warning_count = sum(
            len(r.state.get("report_warnings", []) or []) for r in results
        )
        theme_counts: Dict[str, int] = {}
        for result in results:
            for theme in ResearchAgent._extract_themes(
                "\n".join(
                    [
                        result.state.get("market_report", ""),
                        result.state.get("fundamentals_report", ""),
                        result.state.get("news_report", ""),
                        result.state.get("sentiment_report", ""),
                    ]
                )
            ):
                theme_counts[theme] = theme_counts.get(theme, 0) + 1
        top_themes = ", ".join(
            f"{theme} ({count})"
            for theme, count in sorted(theme_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        ) or "None"
        top_confidence = ResearchAgent._top_tickers_by_metric(results, "confidence")
        top_quality = ResearchAgent._top_tickers_by_metric(results, "data_quality_score")
        top_warnings = ResearchAgent._top_tickers_by_warnings(results)
        rank_confidence = ResearchAgent._top_ticker_rows(results, "confidence")
        rank_quality = ResearchAgent._top_ticker_rows(results, "data_quality_score")
        rank_warnings = ResearchAgent._top_warning_rows(results)
        rank_len = max(len(rank_confidence), len(rank_quality), len(rank_warnings), 1)
        ranking_rows = []
        for idx in range(rank_len):
            conf = rank_confidence[idx] if idx < len(rank_confidence) else None
            qual = rank_quality[idx] if idx < len(rank_quality) else None
            warn = rank_warnings[idx] if idx < len(rank_warnings) else None
            conf_label = f"{conf[1]} ({conf[0]:.1f})" if conf else "-"
            qual_label = f"{qual[1]} ({qual[0]:.1f})" if qual else "-"
            warn_label = f"{warn[1]} ({int(warn[0])})" if warn else "-"
            ranking_rows.append(
                f"<tr><td>{idx + 1}</td><td>{conf_label}</td><td>{qual_label}</td><td>{warn_label}</td></tr>"
            )

        with open(summary_path, "w", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["summary_key", "value"])
            writer.writerow(["snapshot_filter", filter_label])
            writer.writerow(["batch_total", batch_total])
            writer.writerow(["summary_total", summary_total])
            writer.writerow(["buy", buy_count])
            writer.writerow(["sell", sell_count])
            writer.writerow(["hold", hold_count])
            writer.writerow(["errors", error_count])
            writer.writerow(["sec_snapshots", sec_count])
            writer.writerow(["transcript_snapshots", transcript_count])
            writer.writerow(["avg_duration_seconds", round(avg_duration, 1)])
            writer.writerow(["duration_p50_seconds", round(duration_p50, 1)])
            writer.writerow(["duration_p75_seconds", round(duration_p75, 1)])
            writer.writerow(["duration_p90_seconds", round(duration_p90, 1)])
            writer.writerow(["avg_confidence", round(avg_confidence, 2)])
            writer.writerow(["avg_data_quality", round(avg_quality, 2)])
            writer.writerow(["qa_warning_count", qa_warning_count])
            writer.writerow(["top_themes", top_themes])
            writer.writerow(["top_confidence_tickers", top_confidence or "None"])
            writer.writerow(["top_quality_tickers", top_quality or "None"])
            writer.writerow(["top_warning_tickers", top_warnings or "None"])
            writer.writerow(["watchlist_brief_path", brief_name])
            writer.writerow(["watchlist_brief_json_path", brief_json_name])
            writer.writerow(["watchlist_brief_csv_path", brief_csv_name])
            writer.writerow([])
            writer.writerow([
                "ticker",
                "decision",
                "duration_seconds",
                "themes",
                "sec_snapshot",
                "transcript_snapshot",
                "sec_snapshot_path",
                "transcript_snapshot_path",
                "html_path",
                "pdf_path",
                "analysis_id",
            ])
            for result in results:
                sec_snapshot = "yes" if result.state.get("sec_filings_snapshot") else "no"
                transcript_snapshot = "yes" if result.state.get("earnings_transcript_snapshot") else "no"
                sec_path = snapshot_links.get(result.ticker, {}).get("sec", "") if sec_snapshot == "yes" else ""
                transcript_path = (
                    snapshot_links.get(result.ticker, {}).get("transcript", "")
                    if transcript_snapshot == "yes"
                    else ""
                )
                writer.writerow(
                    [
                        result.ticker,
                        result.decision,
                        f"{result.duration_seconds:.1f}",
                        ResearchAgent._themes_for_result(result),
                        sec_snapshot,
                        transcript_snapshot,
                        sec_path,
                        transcript_path,
                        result.html_path or "",
                        result.pdf_path or "",
                        result.analysis_id or "",
                    ]
                )

        logger.info("Batch summary saved to: %s", summary_path)
        return summary_path

    def _write_batch_summary_only(
        self,
        results: List[ResearchResult],
        date: str,
        summary_path: Optional[str] = None,
        summary_snapshot_filter: Optional[str] = None,
        batch_total: Optional[int] = None,
    ) -> str:
        """Write a summary-only CSV for batch runs (no per-ticker rows)."""
        import csv

        if summary_path is None:
            summaries_dir = os.path.join(self.output_dir, "summaries")
            os.makedirs(summaries_dir, exist_ok=True)
            summary_path = os.path.join(summaries_dir, f"batch_summary_only_{date}.csv")

        os.makedirs(os.path.dirname(summary_path), exist_ok=True)
        snapshots_dir = os.path.join(self.output_dir, "snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)
        snapshot_links = ResearchAgent._write_snapshot_files(self, results, snapshots_dir, date)

        summary_total = len(results)
        batch_total = batch_total if batch_total is not None else summary_total
        filter_label = (summary_snapshot_filter or "all").lower()
        
        # Generate watchlist brief files
        base_name = os.path.splitext(os.path.basename(summary_path))[0]
        name_suffix = base_name[len("batch_summary_only_"):] if base_name.startswith("batch_summary_only_") else date
        brief_path = ResearchAgent._write_watchlist_brief(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_name = os.path.basename(brief_path)
        brief_json_path = ResearchAgent._write_watchlist_brief_json(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_json_name = os.path.basename(brief_json_path)
        brief_csv_path = ResearchAgent._write_watchlist_brief_csv(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_csv_name = os.path.basename(brief_csv_path)

        buy_count = sum(1 for r in results if "BUY" in r.decision.upper())
        sell_count = sum(1 for r in results if "SELL" in r.decision.upper())
        hold_count = sum(1 for r in results if "HOLD" in r.decision.upper())
        error_count = sum(1 for r in results if "ERROR" in r.decision.upper())
        sec_count = sum(1 for r in results if r.state.get("sec_filings_snapshot"))
        transcript_count = sum(1 for r in results if r.state.get("earnings_transcript_snapshot"))
        avg_duration = sum(r.duration_seconds for r in results) / summary_total if summary_total else 0.0
        durations = [r.duration_seconds for r in results if r.duration_seconds is not None]
        duration_p50 = ResearchAgent._percentile(durations, 50.0)
        duration_p75 = ResearchAgent._percentile(durations, 75.0)
        duration_p90 = ResearchAgent._percentile(durations, 90.0)
        avg_confidence = (
            sum((r.state.get("confidence", 0) or 0) for r in results) / summary_total
            if summary_total
            else 0.0
        )
        avg_quality = (
            sum((r.state.get("data_quality_score", 0) or 0) for r in results) / summary_total
            if summary_total
            else 0.0
        )
        qa_warning_count = sum(
            len(r.state.get("report_warnings", []) or []) for r in results
        )
        theme_counts: Dict[str, int] = {}
        for result in results:
            for theme in ResearchAgent._extract_themes(
                "\n".join(
                    [
                        result.state.get("market_report", ""),
                        result.state.get("fundamentals_report", ""),
                        result.state.get("news_report", ""),
                        result.state.get("sentiment_report", ""),
                    ]
                )
            ):
                theme_counts[theme] = theme_counts.get(theme, 0) + 1
        top_themes = ", ".join(
            f"{theme} ({count})"
            for theme, count in sorted(theme_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        ) or "None"
        top_confidence = ResearchAgent._top_tickers_by_metric(results, "confidence")
        top_quality = ResearchAgent._top_tickers_by_metric(results, "data_quality_score")
        top_warnings = ResearchAgent._top_tickers_by_warnings(results)
        sec_paths = [links.get("sec") for links in snapshot_links.values() if links.get("sec")]
        transcript_paths = [
            links.get("transcript") for links in snapshot_links.values() if links.get("transcript")
        ]

        with open(summary_path, "w", newline="") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(["summary_key", "value"])
            writer.writerow(["snapshot_filter", filter_label])
            writer.writerow(["batch_total", batch_total])
            writer.writerow(["summary_total", summary_total])
            writer.writerow(["buy", buy_count])
            writer.writerow(["sell", sell_count])
            writer.writerow(["hold", hold_count])
            writer.writerow(["errors", error_count])
            writer.writerow(["sec_snapshots", sec_count])
            writer.writerow(["transcript_snapshots", transcript_count])
            writer.writerow(["avg_duration_seconds", round(avg_duration, 1)])
            writer.writerow(["duration_p50_seconds", round(duration_p50, 1)])
            writer.writerow(["duration_p75_seconds", round(duration_p75, 1)])
            writer.writerow(["duration_p90_seconds", round(duration_p90, 1)])
            writer.writerow(["avg_confidence", round(avg_confidence, 2)])
            writer.writerow(["avg_data_quality", round(avg_quality, 2)])
            writer.writerow(["qa_warning_count", qa_warning_count])
            writer.writerow(["top_themes", top_themes])
            writer.writerow(["top_confidence_tickers", top_confidence or "None"])
            writer.writerow(["top_quality_tickers", top_quality or "None"])
            writer.writerow(["top_warning_tickers", top_warnings or "None"])
            writer.writerow(["watchlist_brief_path", brief_name])
            writer.writerow(["watchlist_brief_json_path", brief_json_name])
            writer.writerow(["watchlist_brief_csv_path", brief_csv_name])
            writer.writerow(["sec_snapshot_paths", ";".join(sec_paths)])
            writer.writerow(["transcript_snapshot_paths", ";".join(transcript_paths)])

        logger.info("Batch summary-only saved to: %s", summary_path)
        return summary_path

    def _write_batch_metadata_only(
        self,
        results: List[ResearchResult],
        date: str,
        summary_path: Optional[str] = None,
        summary_snapshot_filter: Optional[str] = None,
        batch_total: Optional[int] = None,
    ) -> str:
        """Write metadata-only CSV (no per-ticker rows)."""
        return ResearchAgent._write_batch_summary_only(
            self,
            results,
            date,
            summary_path=summary_path,
            summary_snapshot_filter=summary_snapshot_filter,
            batch_total=batch_total,
        )

    @staticmethod
    def _write_summary_only_from_csv(source_path: str, output_path: str) -> str:
        """Extract summary rows from a batch CSV and write summary-only CSV."""
        import csv

        rows = []
        with open(source_path, "r", newline="") as handle:
            reader = csv.reader(handle)
            for row in reader:
                if not row:
                    break
                rows.append(row)

        if not rows:
            raise ValueError(f"No summary rows found in {source_path}")

        with open(output_path, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows(rows)

        return output_path

    @staticmethod
    def _normalize_snapshot_payload(raw: str) -> Optional[dict]:
        from tradingagents.utils.snapshot_utils import snapshot_text_to_dict

        return snapshot_text_to_dict(raw)

    def _write_snapshot_files(
        self,
        results: List[ResearchResult],
        output_dir: str,
        date: str,
    ) -> Dict[str, Dict[str, str]]:
        """Write snapshot JSON files and return link map by ticker."""
        os.makedirs(output_dir, exist_ok=True)
        link_map: Dict[str, Dict[str, str]] = {}

        for result in results:
            ticker = result.ticker
            link_map[ticker] = {}
            sec_raw = result.state.get("sec_filings_snapshot")
            if sec_raw:
                payload = ResearchAgent._normalize_snapshot_payload(sec_raw)
                if payload is not None:
                    filename = f"{ticker}_sec_snapshot_{date}.json"
                    path = os.path.join(output_dir, filename)
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2)
                    link_map[ticker]["sec"] = filename

            transcript_raw = result.state.get("earnings_transcript_snapshot")
            if transcript_raw:
                payload = ResearchAgent._normalize_snapshot_payload(transcript_raw)
                if payload is not None:
                    filename = f"{ticker}_transcript_snapshot_{date}.json"
                    path = os.path.join(output_dir, filename)
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle, indent=2)
                    link_map[ticker]["transcript"] = filename

        return link_map

    def _write_batch_summary_html(
        self,
        results: List[ResearchResult],
        date: str,
        summary_path: Optional[str] = None,
        summary_snapshot_filter: Optional[str] = None,
        batch_total: Optional[int] = None,
    ) -> str:
        """Write an HTML comparison table for batch runs."""
        if summary_path is None:
            summaries_dir = os.path.join(self.output_dir, "summaries")
            os.makedirs(summaries_dir, exist_ok=True)
            summary_path = os.path.join(summaries_dir, f"batch_summary_{date}.html")

        os.makedirs(os.path.dirname(summary_path), exist_ok=True)

        snapshot_dir = os.path.dirname(summary_path)
        snapshot_links = ResearchAgent._write_snapshot_files(self, results, snapshot_dir, date)

        summary_total = len(results)
        batch_total = batch_total if batch_total is not None else summary_total
        filter_label = (summary_snapshot_filter or "all").lower()
        base_name = os.path.splitext(os.path.basename(summary_path))[0]
        name_suffix = base_name[len("batch_summary_"):] if base_name.startswith("batch_summary_") else date
        brief_path = ResearchAgent._write_watchlist_brief(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_name = os.path.basename(brief_path)
        brief_json_path = ResearchAgent._write_watchlist_brief_json(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_json_name = os.path.basename(brief_json_path)
        brief_csv_path = ResearchAgent._write_watchlist_brief_csv(
            self,
            results,
            date,
            os.path.dirname(summary_path),
            filter_label,
            batch_total,
            summary_total,
            name_suffix,
        )
        brief_csv_name = os.path.basename(brief_csv_path)
        summary_only_name = f"batch_summary_only_{date}.csv"
        summary_meta_name = f"batch_summary_meta_{date}.csv"
        backtest_report_name = f"backtest_runs.html"
        buy_count = sum(1 for r in results if "BUY" in r.decision.upper())
        sell_count = sum(1 for r in results if "SELL" in r.decision.upper())
        hold_count = sum(1 for r in results if "HOLD" in r.decision.upper())
        error_count = sum(1 for r in results if "ERROR" in r.decision.upper())
        sec_count = sum(1 for r in results if r.state.get("sec_filings_snapshot"))
        transcript_count = sum(1 for r in results if r.state.get("earnings_transcript_snapshot"))
        avg_duration = sum(r.duration_seconds for r in results) / summary_total if summary_total else 0.0
        durations = [r.duration_seconds for r in results if r.duration_seconds is not None]
        duration_p50 = ResearchAgent._percentile(durations, 50.0)
        duration_p90 = ResearchAgent._percentile(durations, 90.0)
        avg_confidence = (
            sum((r.state.get("confidence", 0) or 0) for r in results) / summary_total
            if summary_total
            else 0.0
        )
        avg_quality = (
            sum((r.state.get("data_quality_score", 0) or 0) for r in results) / summary_total
            if summary_total
            else 0.0
        )
        qa_warning_count = sum(
            len(r.state.get("report_warnings", []) or []) for r in results
        )
        theme_counts: Dict[str, int] = {}
        for result in results:
            for theme in ResearchAgent._extract_themes(
                "\n".join(
                    [
                        result.state.get("market_report", ""),
                        result.state.get("fundamentals_report", ""),
                        result.state.get("news_report", ""),
                        result.state.get("sentiment_report", ""),
                    ]
                )
            ):
                theme_counts[theme] = theme_counts.get(theme, 0) + 1
        top_themes = ", ".join(
            f"{theme} ({count})"
            for theme, count in sorted(theme_counts.items(), key=lambda item: item[1], reverse=True)[:5]
        ) or "None"
        top_confidence = ResearchAgent._top_tickers_by_metric(results, "confidence")
        top_quality = ResearchAgent._top_tickers_by_metric(results, "data_quality_score")
        top_warnings = ResearchAgent._top_tickers_by_warnings(results)

        # Comparative ranking rows for the HTML table
        rank_confidence = ResearchAgent._top_ticker_rows(results, "confidence")
        rank_quality = ResearchAgent._top_ticker_rows(results, "data_quality_score")
        rank_warnings = ResearchAgent._top_warning_rows(results)
        rank_len = max(len(rank_confidence), len(rank_quality), len(rank_warnings), 1)
        ranking_rows = []
        for idx in range(rank_len):
            conf = rank_confidence[idx] if idx < len(rank_confidence) else None
            qual = rank_quality[idx] if idx < len(rank_quality) else None
            warn = rank_warnings[idx] if idx < len(rank_warnings) else None
            conf_label = f"{conf[1]} ({conf[0]:.1f})" if conf else "-"
            qual_label = f"{qual[1]} ({qual[0]:.1f})" if qual else "-"
            warn_label = f"{warn[1]} ({int(warn[0])})" if warn else "-"
            ranking_rows.append(
                f"<tr><td>{idx + 1}</td><td>{conf_label}</td><td>{qual_label}</td><td>{warn_label}</td></tr>"
            )

        rows = []
        for result in results:
            html_link = f'<a href="{result.html_path}">HTML</a>' if result.html_path else "N/A"
            pdf_link = f'<a href="{result.pdf_path}">PDF</a>' if result.pdf_path else "N/A"
            sec_flag = "Yes" if result.state.get("sec_filings_snapshot") else "No"
            transcript_flag = "Yes" if result.state.get("earnings_transcript_snapshot") else "No"
            summary_links = []
            if result.state.get("sec_filings_snapshot"):
                sec_link = snapshot_links.get(result.ticker, {}).get("sec")
                if sec_link:
                    summary_links.append(f'<a href="{sec_link}">SEC</a>')
            if result.state.get("earnings_transcript_snapshot"):
                transcript_link = snapshot_links.get(result.ticker, {}).get("transcript")
                if transcript_link:
                    summary_links.append(f'<a href="{transcript_link}">Transcript</a>')
            summary_label = ", ".join(summary_links) if summary_links else "None"
            rows.append(
                f"<tr><td>{result.ticker}</td><td>{result.decision}</td>"
                f"<td>{result.duration_seconds:.1f}s</td><td>{sec_flag}</td><td>{transcript_flag}</td>"
                f"<td>{summary_label}</td><td>{html_link}</td><td>{pdf_link}</td><td>{result.analysis_id or ''}</td></tr>"
            )

        base_name = os.path.splitext(os.path.basename(summary_path))[0]
        if base_name.startswith("batch_summary_"):
            name_suffix = base_name[len("batch_summary_"):]
        else:
            name_suffix = date

        summary_only_name = f"batch_summary_only_{name_suffix}.csv"
        summary_meta_name = f"batch_summary_meta_{name_suffix}.csv"
        backtest_report_name = "backtest_runs.html"
        backtest_report_link = (
            f'<a href="{backtest_report_name}">{backtest_report_name}</a>'
            if os.path.exists(os.path.join(os.path.dirname(summary_path), backtest_report_name))
            else "N/A"
        )
        multi_links = (
            f' | <a href="batch_summary_{date}_sec.html">SEC summary</a>'
            f' | <a href="batch_summary_{date}_transcript.html">Transcript summary</a>'
        )
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Batch Summary {date}</title>
  <style>
    body {{ font-family: Arial, sans-serif; padding: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    .meta {{ color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>Batch Summary - {date}</h1>
  <p class="meta">
    Snapshot filter: {filter_label} | Total: {summary_total} (batch {batch_total}) | BUY: {buy_count} |
    SELL: {sell_count} | HOLD: {hold_count} | Errors: {error_count} |
    SEC snapshots: {sec_count} | Transcript snapshots: {transcript_count} | Avg duration: {avg_duration:.1f}s |
    P50: {duration_p50:.1f}s | P90: {duration_p90:.1f}s | Avg confidence: {avg_confidence:.1f}% |
    Avg quality: {avg_quality:.1f}% | QA warnings: {qa_warning_count}
  </p>
  <p class="meta">
    Top themes: {top_themes} | Top confidence: {top_confidence or "None"} |
    Top quality: {top_quality or "None"} | Most warnings: {top_warnings or "None"}
  </p>
  <h2>Comparative Rankings</h2>
  <table>
    <thead>
      <tr>
        <th>Rank</th>
        <th>Top confidence</th>
        <th>Top quality</th>
        <th>Most warnings</th>
      </tr>
    </thead>
    <tbody>
      {"".join(ranking_rows)}
    </tbody>
  </table>
  <p class="meta">
    Summary-only CSV: <a href="{summary_only_name}">{summary_only_name}</a> |
    Summary metadata CSV: <a href="{summary_meta_name}">{summary_meta_name}</a> |
    Watchlist brief: <a href="{brief_name}">{brief_name}</a> |
    Watchlist JSON: <a href="{brief_json_name}">{brief_json_name}</a> |
    Watchlist CSV: <a href="{brief_csv_name}">{brief_csv_name}</a> |
    Backtest report: {backtest_report_link}{multi_links}
  </p>
  <table>
    <thead>
      <tr>
        <th>Ticker</th>
        <th>Decision</th>
        <th>Duration</th>
        <th>SEC Snapshot</th>
        <th>Transcript Snapshot</th>
        <th>Snapshot Links</th>
        <th>HTML</th>
        <th>PDF</th>
        <th>Analysis ID</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>
"""

        with open(summary_path, "w", encoding="utf-8") as html_file:
            html_file.write(html)

        logger.info("Batch HTML summary saved to: %s", summary_path)
        return summary_path
    
    def get_history(self, ticker: str = None, limit: int = 20) -> List[Analysis]:
        """Get historical analyses.
        
        Args:
            ticker: Optional ticker filter
            limit: Maximum number of results
            
        Returns:
            List of Analysis objects
        """
        if ticker:
            return self.db.get_analyses_by_ticker(ticker.upper(), limit)
        return self.db.get_recent_analyses(limit)
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get database statistics."""
        return self.db.get_statistics()
    
    def export_history(self, filepath: str, ticker: str = None) -> str:
        """Export analyses to CSV.
        
        Args:
            filepath: Output CSV path
            ticker: Optional ticker filter
            
        Returns:
            Path to exported file
        """
        return self.db.export_to_csv(filepath, ticker)

    def run_backtest(
        self,
        ticker: str = None,
        limit: int = 50,
        lookahead_days: Optional[List[int]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        slippage_bps: float = 0.0,
        transaction_cost_bps: float = 0.0,
    ) -> Dict[str, int]:
        """Run backtesting on stored analyses."""
        from .backtesting import BacktestEngine

        engine = BacktestEngine(self.db.db_path)
        return engine.run(
            ticker=ticker,
            limit=limit,
            lookahead_days=lookahead_days,
            start_date=start_date,
            end_date=end_date,
            slippage_bps=slippage_bps,
            transaction_cost_bps=transaction_cost_bps,
        )
    
    def update_config(self, **kwargs):
        """Update configuration and reinitialize graph.
        
        Args:
            **kwargs: Configuration key-value pairs
        """
        self.config.update(kwargs)
        self._init_graph()


# Convenience function for quick analysis
def quick_analyze(
    ticker: str,
    date: str = None,
    config: Dict[str, Any] = None,
) -> ResearchResult:
    """Run a quick analysis with minimal setup.
    
    Args:
        ticker: Stock ticker symbol
        date: Analysis date (default: today)
        config: Optional custom configuration
        
    Returns:
        ResearchResult with decision and reports
    """
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    
    agent = ResearchAgent(
        config=config,
        auto_report=True,
        auto_save=True,
        debug=False,
    )
    
    return agent.analyze(ticker, date)
