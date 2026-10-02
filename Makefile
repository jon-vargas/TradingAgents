.DEFAULT_GOAL := help
.PHONY: help ui serve stop restart status logs health limiter-status \
        auto-discovery-status auto-discovery-run auto-discovery-run-wait auto-watchlists \
        builtins-refresh-propose builtins-refresh-proposals builtins-refresh-apply builtins-deprecate-apply \
        watchlist-metrics watchlist-migration-precheck watchlist-parity-diff \
        analyze batch-analyze batch-summary-only institutional-data \
        watchlists screen screen-watchlist screen-all screen-all-async screen-all-status screen-all-watch screen-all-prewarm \
        evict-delisted-tickers-dry evict-delisted-tickers \
        backfill-asset-class-dry backfill-asset-class \
        backfill-orphan-analyses-dry backfill-orphan-analyses \
        scan-metrics-prune-dry scan-metrics-prune scan-p95-reset-dry scan-p95-reset \
        upcoming-events \
        screen-backtest movers-scan movers-queue-top movers-alerts \
        long-horizon-prewarm long-horizon-scan long-horizon-status long-horizon-runs long-horizon-underwrite long-horizon-allocate \
        portfolio-risk \
        export-tradingview-latest export-tradingview-run \
        backtest backtest-report calibration-report \
        calibration-by-decision calibration-by-factor reverse-dcf-accuracy calibration-all \
        alerts macro-status \
        qa qa-report qa-report-update qa-report-tests qa-screen-all-smoke qa-healthcare-watchlist-smoke qa-manual test lint stabilization-gate \
        diff \
        setup verify perplexity-smoke clean clean-reports clean-cache clean-cache-type clean-logs clean-all \
        clean-eval clean-results clean-old clean-auto-watchlists storage-report \
        db-info db-backup db-reset db-vacuum db-cleanup-backups \
        profiles

PYTHON ?= python
BACKTEST_ARGS ?=
BACKTEST_REPORT_ARGS ?=
CALIBRATION_ARGS ?=
BATCH_ARGS ?=
PORT ?= 8000
FORCE ?= 0
FORCE_REASON ?=
AUTO_DISCOVERY_TIMEOUT ?= 900
AUTO_DISCOVERY_POLL ?= 5

help:
	@echo ""
	@echo "  TradingAgents — Command Reference"
	@echo "  =================================="
	@echo ""
	@echo "=== Setup ==="
	@echo "  make setup                      Run quick setup script"
	@echo "  make verify                     Verify environment & API keys"
	@echo "  make perplexity-smoke TICKER=    Live Perplexity Agent API smoke (needs PERPLEXITY_API_KEY)"
	@echo ""
	@echo "=== Web UI ==="
	@echo "  make ui                         Start web UI (http://localhost:$(PORT))"
	@echo "  make serve                      Alias for 'make ui'"
	@echo "  make stop                       Stop running web UI"
	@echo "  make restart                    Restart web UI"
	@echo "  make status                     Check if web UI is running"
	@echo "  make health                     Query the /health/ready endpoint"
	@echo "  make limiter-status             Show yfinance rate-limiter breaker state"
	@echo "  make logs                       Tail structured log file (logs/tradingagents.log)"
	@echo ""
	@echo "=== Auto-Discovery (Manual) ==="
	@echo "  make auto-discovery-status      Show manual auto-discovery status + budget"
	@echo "  make auto-discovery-run         Trigger manual auto-discovery run"
	@echo "  make auto-discovery-run-wait    Trigger and wait for completion (polling)"
	@echo "  make auto-watchlists            List current auto-generated watchlists"
	@echo ""
	@echo "=== Built-In Refresh (Proposal Workflow) ==="
	@echo "  make builtins-refresh-propose   Generate built-in refresh proposal"
	@echo "  make builtins-refresh-proposals List recent built-in refresh proposals"
	@echo "  make builtins-refresh-apply ID=7 [FORCE=1 FORCE_REASON=\"...\"] Apply a proposal atomically"
	@echo "  make builtins-deprecate-apply   Remove deprecated built-in watchlists"
	@echo ""
	@echo "=== Watchlist Universe (Registry) ==="
	@echo "  make watchlist-metrics                                 Show universe size/materialization/p95 metrics"
	@echo "  make watchlist-migration-precheck                      Precheck legacy watchlist cleanup"
	@echo "  make watchlist-parity-diff LEFT=\"S&P 500\" RIGHT=\"S&P 500\" Compare two watchlists' tickers"
	@echo ""
	@echo "=== Single Analysis ==="
	@echo "  make analyze TICKER=NVDA [DATE=...] [MODE=standard] [RISK=growth] [PROFILE=high_growth]"
	@echo "    Modes:    quick | standard | deep"
	@echo "    Risks:    aggressive | growth | conservative"
	@echo "  make institutional-data TICKER=NVDA   Show DCF, peer comps & scenarios inline (no server needed)"
	@echo ""
	@echo "=== Batch Analysis ==="
	@echo "  make batch-analyze TICKERS=\"NVDA AAPL\" DATE=YYYY-MM-DD [MODE=...] [RISK=...] [PROFILE=...]"
	@echo "  make batch-summary-only SUMMARY=... OUTPUT=..."
	@echo ""
	@echo "=== Screening ==="
	@echo "  make watchlists                 List all available watchlists"
	@echo "  make screen WATCHLIST=\"AAPL MSFT\" [PRESET=momentum_hunter]"
	@echo "  make screen-watchlist NAME=\"AI Infrastructure Metals\" [PRESET=...] [MODE=reversal_buildup|base_coil]"
	@echo "  make screen-watchlist ID=5 [PRESET=value_fisher] [MODE=reversal_buildup|base_coil]"
	@echo "  make screen-all                 Scan ALL watchlists (CLI, blocks until done — use for <1k ticker universes)"
	@echo "  make screen-all-async [MODE=reversal_buildup|early_momentum|base_coil]  Submit scan-all to the jobs API (non-blocking)"
	@echo "  make screen-all-status ID=...   Poll a scan-all job by id"
	@echo "  make screen-all-watch ID=...    Follow a scan-all job until terminal state"
	@echo "  make screen-all-prewarm [PREWARM_ARGS='--batch-size 60 --date YYYY-MM-DD']  Warm screener DataCache (union universe)"
	@echo "  make backfill-asset-class-dry   Preview ticker_metadata rows missing asset_class"
	@echo "  make backfill-asset-class [BACKFILL_LIMIT=N] [BACKFILL_WORKERS=6]  Backfill asset_class + country"
	@echo "  make evict-delisted-tickers-dry Preview tickers marked delisted/stale by ticker_health"
	@echo "  make evict-delisted-tickers EVICT_CONFIRM=1  Evict those tickers from ticker_metadata"
	@echo "  make upcoming-events [TICKER=AAPL] [DAYS=90]  Show upcoming events from event_calendar (no server)"
	@echo "  make scan-metrics-prune-dry     Preview runtime_metrics samples eligible for pruning"
	@echo "  make scan-metrics-prune PRUNE_CONFIRM=1  Trim historical samples so p95 reflects current behaviour"
	@echo "  make scan-p95-reset-dry         Preview keep-newest reset for scan_all_duration_seconds"
	@echo "  make scan-p95-reset PRUNE_CONFIRM=1  Reset scan-all p95 baseline (keep KEEP_NEWEST samples)"
	@echo "  make movers-scan                Run Movers Intelligence dual-horizon scan"
	@echo "  make movers-queue-top N=5 HORIZON=short|medium   Queue top movers for analysis"
	@echo "  make movers-alerts HORIZON=short|medium TOP=5    Create alerts from latest movers run"
	@echo "  make long-horizon-prewarm [PREWARM_MAX=600 PREWARM_SKIP_INFO=false]  Warm expanded-universe caches"
	@echo "  make long-horizon-scan LH_HORIZON=long_6to12m TOP=30 [LH_USE_EXPANDED=true] [WL_ID=5|TICKERS=\"AAPL MSFT\"]"
	@echo "  make long-horizon-status        Show long-horizon service status/guardrails"
	@echo "  make long-horizon-runs          List recent long-horizon runs"
	@echo "  make long-horizon-underwrite RUN_ID=123 [TOP=10]  Underwrite top names for run"
	@echo "  make long-horizon-allocate RUN_ID=123            Compute constrained allocation"
	@echo "  make portfolio-risk RUN_ID=123  Show portfolio risk summary for a long-horizon run (beta, HHI, factors)"
	@echo "  make export-tradingview-latest [TOP=20] [FORMAT=txt|csv] [OUT=...]"
	@echo "  make export-tradingview-run RUN_ID=123 [TOP=20] [FORMAT=txt|csv] [OUT=...]"
	@echo "  make screen-backtest RUN_ID=1   Backtest a screening run"
	@echo "    Presets: momentum_hunter | value_fisher | earnings_play | smart_money_tracker"
	@echo "             dividend_income | small_cap_growth | commodity_cyclical | short_squeeze"
	@echo ""
	@echo "=== Investment Profiles ==="
	@echo "  make profiles                   Show available investment profiles"
	@echo ""
	@echo "=== Backtesting & Calibration ==="
	@echo "  make backtest                   Run backtest on stored analyses"
	@echo "  make backtest-report            Generate backtest runs report"
	@echo "  make calibration-report         Generate confidence calibration report (global)"
	@echo "  make calibration-by-decision    Calibration sliced by BUY/HOLD/SELL (no server needed)"
	@echo "  make calibration-by-factor      Calibration sliced by factor family (no server needed)"
	@echo "  make reverse-dcf-accuracy       Implied vs consensus growth error distribution (no server needed)"
	@echo "  make calibration-all            Run all three calibration views in sequence"
	@echo ""
	@echo "=== Alerts & Macro ==="
	@echo "  make alerts                     Show alert rules and recent alerts"
	@echo "  make macro-status               Show current macro environment (credit, breadth, regime)"
	@echo ""
	@echo "=== Reports ==="
	@echo "  make diff TICKER=NVDA A=2026-01-01 B=2026-02-01"
	@echo ""
	@echo "=== QA & Testing ==="
	@echo "  make test                       Run all tests"
	@echo "  make lint                       Run Python linters (ruff if installed)"
	@echo "  make stabilization-gate         Run movers+refresh+long-horizon stabilization tests"
	@echo "  make qa                         Run report QA + tests"
	@echo "  make qa-report                  Run fast report QA"
	@echo "  make qa-report-update           Update report QA goldens"
	@echo "  make qa-report-tests            Run unit test discovery under tests/"
	@echo "  make qa-manual                  List manual QA scripts under scripts/qa/"
	@echo "  make qa-screen-all-smoke        Wall-clock scan smoke (Sector ETFs + NASDAQ 100)"
	@echo "  make qa-healthcare-watchlist-smoke  Scan smoke for Healthcare & Life Sciences (~105 names)"
	@echo ""
	@echo "=== Database ==="
	@echo "  make db-info                    Show database stats (tables, row counts)"
	@echo "  make db-backup                  Backup research.db; auto-prunes to KEEP most recent (default: $(KEEP))"
	@echo "  make db-reset                   Delete database (confirmation required)"
	@echo ""
	@echo "=== Cleanup ==="
	@echo "  make clean                      Clear API data cache (JSON + CSV)"
	@echo "  make clean-cache-type TYPE=estimate_revisions   Clear specific cache type"
	@echo "    Types: estimate_revisions, rating_changes, earnings_quality, intrinsic_value,"
	@echo "           catalyst_pipeline, weekly_technicals, sector_breadth, macro_snapshot,"
	@echo "           ticker_info, peer_comps, corporate_actions, insider_net_buy"
	@echo "  make clean-reports              Remove all generated report files"
	@echo "  make clean-eval                 Remove eval_results/ state logs"
	@echo "  make clean-results              Remove legacy results/ output (one-time)"
	@echo "  make clean-logs                 Clear log files"
	@echo "  make clean-old DAYS=90          Age-based cleanup across reports, eval, cache, backups"
	@echo "  make clean-auto-watchlists      Delete all source=auto watchlists (confirmation required)"
	@echo "  make clean-all                  Clear cache + eval + logs + backups + __pycache__"
	@echo ""
	@echo "=== Storage ==="
	@echo "  make storage-report             Print per-directory file counts and disk usage"
	@echo "  make db-vacuum                  Run VACUUM + ANALYZE on research.db"
	@echo "  make db-cleanup-backups KEEP=5  Prune old database backup files"
	@echo ""
	@echo "Optional: PORT=8000 TOP=20 N=5 HORIZON=short LH_HORIZON=long_6to12m LH_USE_EXPANDED=true WL_ID=... TICKERS=\"AAPL MSFT\""
	@echo "          RUN_ID=... INCLUDE_LOSERS=true PREWARM_MAX=600 PREWARM_SKIP_INFO=false FORCE=1 KEEP=5 BACKTEST_ARGS=... BATCH_ARGS=..."
	@echo "          LEFT=\"S&P 500\" RIGHT=\"S&P 500\" (parity-diff), AUTO_DISCOVERY_TIMEOUT=900, AUTO_DISCOVERY_POLL=5"
	@echo ""

# =============================================================================
# SETUP & VERIFICATION
# =============================================================================

setup:
	@echo "Running quick setup..."
	@bash quick_setup.sh

verify:
	@echo "Verifying environment setup..."
	$(PYTHON) scripts/verify_setup.py

TICKER ?= AAPL
perplexity-smoke:
	@echo "Perplexity Agent API smoke for $(TICKER)..."
	@$(PYTHON) -c "from tradingagents.dataflows.perplexity_api import get_deep_research, get_sec_filings_snapshot; t='$(TICKER)'; d=get_deep_research(t, t); s=get_sec_filings_snapshot(t, t); print('deep_research chars', len(d)); print(s[:400])"

# =============================================================================
# WEB UI COMMANDS
# =============================================================================

ui:
	@if lsof -ti:$(PORT) > /dev/null 2>&1; then \
		echo "Port $(PORT) is already in use"; \
		echo "   Run 'make stop' first, or use 'make restart'"; \
		exit 1; \
	fi
	@echo "Starting TradingAgents Web UI at http://localhost:$(PORT)"
	@echo "Logs: logs/tradingagents.log  (tail with 'make logs')"
	@echo "Press Ctrl+C to stop, or use 'make stop' from another terminal"
	@DYLD_LIBRARY_PATH=/opt/homebrew/lib uvicorn webapp.app:app --reload --port $(PORT)

serve: ui

stop:
	@echo "Stopping web UI on port $(PORT)..."
	@if lsof -ti:$(PORT) > /dev/null 2>&1; then \
		lsof -ti:$(PORT) | xargs kill -15 2>/dev/null; \
		sleep 1; \
		if lsof -ti:$(PORT) > /dev/null 2>&1; then \
			echo "Graceful shutdown failed, forcing..."; \
			lsof -ti:$(PORT) | xargs kill -9 2>/dev/null; \
		fi; \
		echo "Stopped"; \
	else \
		echo "No server running on port $(PORT)"; \
	fi

restart: stop
	@sleep 1
	@$(MAKE) ui

status:
	@if lsof -ti:$(PORT) > /dev/null 2>&1; then \
		echo "Web UI is running on port $(PORT)"; \
		echo "  PID(s): $$(lsof -ti:$(PORT) | tr '\n' ' ')"; \
		echo "  URL: http://localhost:$(PORT)"; \
	else \
		echo "Web UI is not running on port $(PORT)"; \
	fi

health:
	@echo "Checking health endpoint..."
	@curl -sf http://localhost:$(PORT)/health/ready 2>/dev/null | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

limiter-status:
	@echo "Fetching yfinance limiter status from /health/ready..."
	@curl -sS http://localhost:$(PORT)/health/ready 2>/dev/null | $(PYTHON) -c "\
import sys, json; \
d = json.load(sys.stdin); \
checks = d.get('checks', {}); \
state = checks.get('yfinance_limiter', 'unknown'); \
trips = checks.get('yfinance_limiter_total_trips', 0); \
print(f'  yfinance_limiter:      {state}'); \
print(f'  total_trips (session): {trips}')" \
		|| echo "FAILED — is the server running? Try 'make status'"

logs:
	@if [ -f "logs/tradingagents.log" ]; then \
		echo "Tailing logs/tradingagents.log (Ctrl+C to stop)..."; \
		echo "---"; \
		tail -f logs/tradingagents.log; \
	else \
		echo "No log file found at logs/tradingagents.log"; \
		echo "Logs are created when the web UI starts. Try 'make ui' first."; \
	fi

auto-discovery-status:
	@echo "Fetching auto-discovery status..."
	@curl -sf "http://localhost:$(PORT)/api/screening/auto-discovery/status" 2>/dev/null | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

auto-discovery-run:
	@echo "Triggering manual auto-discovery run..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/screening/auto-discovery/run"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

auto-discovery-run-wait:
	@echo "Triggering auto-discovery and waiting for completion..."
	@tmp_run=$$(mktemp); \
	code=$$(curl -sS -o "$$tmp_run" -w "%{http_code}" -X POST "http://localhost:$(PORT)/api/screening/auto-discovery/run"); \
	echo "Trigger response ($$code):"; \
	cat "$$tmp_run" | $(PYTHON) -m json.tool 2>/dev/null || cat "$$tmp_run"; \
	if [ "$$code" != "200" ] && [ "$$code" != "409" ]; then rm -f "$$tmp_run"; exit 1; fi; \
	rm -f "$$tmp_run"; \
	start=$$(date +%s); \
	while true; do \
		status_json=$$(curl -sS "http://localhost:$(PORT)/api/screening/auto-discovery/status"); \
		running=$$(printf "%s" "$$status_json" | $(PYTHON) -c "import sys,json; print('1' if json.load(sys.stdin).get('running') else '0')"); \
		if [ "$$running" = "0" ]; then \
			echo "Completed summary:"; \
			printf "%s" "$$status_json" | $(PYTHON) -c "import sys,json; s=json.load(sys.stdin); r=s.get('last_result') or {}; out={'run_id':s.get('run_id'),'last_started_at':s.get('last_started_at'),'last_finished_at':s.get('last_finished_at'),'created_count':r.get('created_count',0),'renewed_count':r.get('renewed_count',0),'expired_count':r.get('expired_count',0),'stale_count':r.get('stale_count',0),'skipped_budget':r.get('skipped_budget',0),'duration_sec':r.get('duration_sec',0),'errors':r.get('errors',[])}; print(json.dumps(out, indent=2))"; \
			break; \
		fi; \
		now=$$(date +%s); \
		if [ $$((now - start)) -ge $(AUTO_DISCOVERY_TIMEOUT) ]; then \
			echo "Timed out after $(AUTO_DISCOVERY_TIMEOUT)s waiting for completion."; \
			exit 1; \
		fi; \
		sleep $(AUTO_DISCOVERY_POLL); \
	done

auto-watchlists:
	@echo "Listing source=auto watchlists from API..."
	@curl -sS "http://localhost:$(PORT)/api/watchlists" | $(PYTHON) -c "import sys,json; data=json.load(sys.stdin); auto=[w for w in data if w.get('source')=='auto']; print(f'Auto watchlists: {len(auto)}'); [print(f\"- id={w.get('id')} name={w.get('name')} tickers={w.get('ticker_count',0)} expires_at={w.get('expires_at')}\") for w in auto]"

builtins-refresh-propose:
	@echo "Generating built-in refresh proposal..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/watchlists/builtins/refresh/propose"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

builtins-refresh-proposals:
	@echo "Listing built-in refresh proposals..."
	@curl -sS "http://localhost:$(PORT)/api/watchlists/builtins/refresh/proposals" | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

builtins-refresh-apply:
	@[ -n "$(ID)" ] || (echo "ID is required. Example: make builtins-refresh-apply ID=7"; exit 1)
	@echo "Applying built-in refresh proposal #$(ID)..."
	@force_apply=false; \
	if [ "$(FORCE)" = "1" ]; then force_apply=true; fi; \
	reason=$$(printf '%s' "$(FORCE_REASON)" | sed 's/"/\\"/g'); \
	resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/watchlists/builtins/refresh/proposals/$(ID)/apply" \
		-H "Content-Type: application/json" \
		-d "{\"force_apply\": $$force_apply, \"force_reason\": \"$$reason\"}"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

builtins-deprecate-apply:
	@echo "Applying deprecated built-in cleanup..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/watchlists/builtins/deprecate/apply"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

# =============================================================================
# WATCHLIST UNIVERSE (REGISTRY) DIAGNOSTICS
# =============================================================================

LEFT ?=
RIGHT ?=

watchlist-metrics:
	@echo "Fetching watchlist universe metrics..."
	@curl -sS "http://localhost:$(PORT)/api/watchlists/universe/metrics" | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

watchlist-migration-precheck:
	@echo "Running watchlist migration precheck..."
	@curl -sS "http://localhost:$(PORT)/api/watchlists/migration/precheck" | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

watchlist-parity-diff:
	@[ -n "$(LEFT)" ] || (echo "LEFT is required. Example: make watchlist-parity-diff LEFT=\"S&P 500\" RIGHT=\"S&P 500\""; exit 1)
	@[ -n "$(RIGHT)" ] || (echo "RIGHT is required. Example: make watchlist-parity-diff LEFT=\"S&P 500\" RIGHT=\"S&P 500\""; exit 1)
	@echo "Comparing watchlists: '$(LEFT)' vs '$(RIGHT)'..."
	@$(PYTHON) -c "import urllib.parse, sys; print(urllib.parse.urlencode({'left': '$(LEFT)', 'right': '$(RIGHT)'}))" | { \
		read -r qs; \
		curl -sS "http://localhost:$(PORT)/api/watchlists/parity-diff?$$qs" | $(PYTHON) -m json.tool 2>/dev/null \
			|| echo "FAILED — is the server running? Try 'make status'"; \
	}

# Preview tickers flagged as likely-delisted by ticker_health. Safe to run any time.
evict-delisted-tickers-dry:
	@echo "Previewing stale/delisted tickers (dry-run)..."
	@$(PYTHON) scripts/evict_delisted_tickers.py $(EVICT_ARGS)

# Actually evict stale/delisted tickers from ticker_metadata.
# Requires confirmation via EVICT_CONFIRM=1 to prevent accidental data loss.
evict-delisted-tickers:
	@[ "$(EVICT_CONFIRM)" = "1" ] || { \
		echo "Refusing to evict without confirmation."; \
		echo "Re-run with: make evict-delisted-tickers EVICT_CONFIRM=1"; \
		echo "First preview with: make evict-delisted-tickers-dry"; \
		exit 2; \
	}
	@$(PYTHON) scripts/evict_delisted_tickers.py --apply $(EVICT_ARGS)

# Show upcoming events from the event_calendar table (no server required).
# Optional: TICKER=AAPL to filter to a single ticker. DAYS sets the horizon (default 90).
upcoming-events:
	@$(PYTHON) -c "\
from tradingagents.reporting.database import get_db; \
import json; \
db = get_db('$(DB_PATH)'); \
ticker = '$(TICKER)'.upper() if '$(TICKER)' else None; \
events = db.get_upcoming_events(ticker=ticker, max_days=$(DAYS), limit=50); \
print(); \
label = f'for {ticker}' if ticker else 'across all tracked tickers'; \
print(f'  Upcoming Events {label} (next $(DAYS) days)'); \
print('  ' + '='*50); \
if not events: \
    print('  None tracked.'); \
else: \
    for ev in events: \
        days = round(ev.get('days_to_event', 0)); \
        etype = ev.get('event_type','').replace('_',' ').title(); \
        lbl = ev.get('label', etype); \
        dt  = str(ev.get('event_date',''))[:10]; \
        t   = ev.get('ticker','?'); \
        print(f'  {days:>3}d  {t:<6}  {lbl:<30} {dt}'); \
print(); \
"

# Backfill asset_class + country on existing ticker_metadata rows whose
# asset_class IS NULL. Required one-time after the universe-broadening
# pass so ETFs/ADRs take the right preset and risk-score branch
# immediately instead of drifting in over the 7-day stale-ticker TTL.
# Tunables:
#   BACKFILL_LIMIT=N       — process at most N rows (default: all)
#   BACKFILL_WORKERS=N     — yfinance fetch concurrency (default 6)
#   BACKFILL_CHUNK=N       — tickers per batch (default 200)
#   BACKFILL_SLEEP=N       — seconds between batches (default 3.0)
BACKFILL_LIMIT ?=
BACKFILL_WORKERS ?= 6
BACKFILL_CHUNK ?= 200
BACKFILL_SLEEP ?= 3.0
backfill-asset-class-dry:
	@$(PYTHON) scripts/backfill_asset_class.py --dry-run \
		$(if $(BACKFILL_LIMIT),--limit $(BACKFILL_LIMIT))

backfill-asset-class:
	@$(PYTHON) scripts/backfill_asset_class.py \
		--workers $(BACKFILL_WORKERS) \
		--chunk-size $(BACKFILL_CHUNK) \
		--chunk-sleep $(BACKFILL_SLEEP) \
		$(if $(BACKFILL_LIMIT),--limit $(BACKFILL_LIMIT))

# Preview / prune historical runtime_metrics samples so the QA-gate p95 metric
# converges to the new baseline after a perf fix lands. Defaults to a dry-run
# unless PRUNE_CONFIRM=1 is supplied. Optional: PRUNE_METRIC=scan_all_duration_seconds
#   make scan-metrics-prune-dry            # default 14-day window
#   make scan-metrics-prune PRUNE_CONFIRM=1 PRUNE_METRIC=scan_all_duration_seconds
PRUNE_DAYS ?= 14
PRUNE_METRIC ?=
scan-metrics-prune-dry:
	@$(PYTHON) scripts/prune_runtime_metrics.py --older-than-days $(PRUNE_DAYS) \
		$(if $(PRUNE_METRIC),--metric $(PRUNE_METRIC))

scan-metrics-prune:
	@[ "$(PRUNE_CONFIRM)" = "1" ] || { \
		echo "Refusing to prune without confirmation."; \
		echo "Re-run with: make scan-metrics-prune PRUNE_CONFIRM=1 [PRUNE_METRIC=...] [PRUNE_DAYS=N]"; \
		echo "Preview first with: make scan-metrics-prune-dry"; \
		exit 2; \
	}
	@$(PYTHON) scripts/prune_runtime_metrics.py --older-than-days $(PRUNE_DAYS) --confirm \
		$(if $(PRUNE_METRIC),--metric $(PRUNE_METRIC))

# Reset a metric's baseline by keeping only the N most-recent samples. Useful
# after a perf fix lands so the QA-gate p95 metric converges immediately.
#   make scan-p95-reset-dry                                 # default: keep latest 1
#   make scan-p95-reset PRUNE_CONFIRM=1 KEEP_NEWEST=1
P95_METRIC ?= scan_all_duration_seconds
KEEP_NEWEST ?= 1
scan-p95-reset-dry:
	@$(PYTHON) scripts/prune_runtime_metrics.py --metric $(P95_METRIC) --keep-newest $(KEEP_NEWEST)

scan-p95-reset:
	@[ "$(PRUNE_CONFIRM)" = "1" ] || { \
		echo "Refusing to prune without confirmation."; \
		echo "Re-run with: make scan-p95-reset PRUNE_CONFIRM=1 [P95_METRIC=...] [KEEP_NEWEST=N]"; \
		echo "Preview first with: make scan-p95-reset-dry"; \
		exit 2; \
	}
	@$(PYTHON) scripts/prune_runtime_metrics.py --metric $(P95_METRIC) --keep-newest $(KEEP_NEWEST) --confirm

# =============================================================================
# SINGLE ANALYSIS
# =============================================================================

TICKER ?=
DATE ?=
MODE ?= standard
RISK ?= growth
PROFILE ?=

analyze:
	@[ -n "$(TICKER)" ] || (echo "TICKER is required. Example: make analyze TICKER=NVDA DATE=2026-02-12 MODE=standard RISK=growth"; exit 1)
	$(PYTHON) scripts/run_analyze.py $(TICKER) \
		$(if $(DATE),--date $(DATE)) \
		--mode $(MODE) \
		--risk-profile $(RISK) \
		$(if $(PROFILE),--investment-profile $(PROFILE))

# =============================================================================
# BATCH ANALYSIS
# =============================================================================

TICKERS ?=
DELAY ?= 60

batch-analyze:
	@[ -n "$(TICKERS)" ] || (echo "TICKERS is required. Example: make batch-analyze TICKERS=\"NVDA AAPL\" DATE=YYYY-MM-DD"; exit 1)
	@[ -n "$(DATE)" ] || (echo "DATE is required. Example: make batch-analyze TICKERS=\"NVDA AAPL\" DATE=YYYY-MM-DD"; exit 1)
	$(PYTHON) scripts/run_batch_analysis.py --tickers $(TICKERS) --date $(DATE) --mode $(MODE) --risk-profile $(RISK) --delay $(DELAY) $(if $(PROFILE),--investment-profile $(PROFILE)) $(BATCH_ARGS)

batch-summary-only:
	@[ -n "$(SUMMARY)" ] || (echo "SUMMARY is required. Example: make batch-summary-only SUMMARY=path/to/batch_summary.csv OUTPUT=out.csv"; exit 1)
	@[ -n "$(OUTPUT)" ] || (echo "OUTPUT is required. Example: make batch-summary-only SUMMARY=path/to/batch_summary.csv OUTPUT=out.csv"; exit 1)
	$(PYTHON) -m cli.main export-batch-summary-only --from-summary $(SUMMARY) --output-path $(OUTPUT)

# Institutional deep-dive: peer comps, DCF + reverse-DCF, scenario analysis (no server needed)
institutional-data:
	@[ -n "$(TICKER)" ] || (echo "TICKER is required. Example: make institutional-data TICKER=NVDA"; exit 1)
	@$(PYTHON) -c "\
import json; \
from tradingagents.dataflows.yfinance_extended import compute_peer_comps, compute_intrinsic_value, compute_scenario_analysis; \
t = '$(TICKER)'.upper(); \
print(); \
print(f'  Institutional Data — {t}'); \
print('  ' + '='*44); \
print(); \
iv = compute_intrinsic_value(t); \
if iv.get('fair_value'): \
    m = iv.get('margin_of_safety_pct', 0); \
    label = 'Undervalued' if m > 0 else 'Overvalued'; \
    print(f'  DCF Fair Value:   \$\${{iv[\"fair_value\"]:.2f}}  ({label} by {{abs(m):.1f}}%)'); \
    a = iv.get('assumptions', {}); \
    print(f'  Assumptions:      WACC {{a.get(\"wacc\",0)*100:.1f}}%  Growth {{a.get(\"growth_rate\",0)*100:.1f}}%'); \
    if iv.get('implied_growth_rate') is not None: \
        ig = iv['implied_growth_rate']*100; \
        vs = iv.get('implied_vs_consensus'); \
        vs_str = f\" ({{vs*100:+.1f}}pp vs consensus)\" if vs is not None else ''; \
        print(f'  Implied Growth:   {{ig:.1f}}%{vs_str}'); \
    grid = iv.get('dcf_sensitivity_grid', {}); \
    if grid: \
        print(f'  Sensitivity Grid: {{len(grid)}} cells computed'); \
else: \
    print(f'  DCF Fair Value:   {{iv.get(\"note\", \"N/A\")}}'); \
print(); \
sa = compute_scenario_analysis(t); \
s = sa.get('scenarios', {}); \
if s: \
    cur = sa.get('current_price', 0); \
    print(f'  Scenarios (current \$\${{cur:.2f}}):'); \
    for case, label2 in [('bull','Bull'),('base','Base'),('bear','Bear')]: \
        sc = s.get(case, {}); \
        price = sc.get('price'); \
        upside = sc.get('upside_pct'); \
        if price: print(f'    {{label2:6s}}  \$\${{price:.2f}}  ({{upside:+.1f}}%)' if upside is not None else f'    {{label2:6s}}  \$\${{price:.2f}}'); \
    if sa.get('blended_fair_value') is not None: \
        print(f'  Blended Target:   \$\${{sa[\"blended_fair_value\"]:.2f}}  ({{sa.get(\"blended_upside_pct\",0):+.1f}}% prob-weighted)'); \
print(); \
pc = compute_peer_comps(t); \
peers = pc.get('peers', []); \
subj = pc.get('subject', {}); \
if peers: \
    print(f'  Peer Comparables ({len(peers)} peers):'); \
    print(f'  {\"Ticker\":<8} {\"P/E\":>6} {\"P/S\":>6} {\"EV/EBIT\":>8}'); \
    print(f'  {\"-\"*32}'); \
    for p in peers[:8]: \
        pe = f'{{p[\"pe\"]:.1f}}x' if p.get('pe') else '—'; \
        ps = f'{{p[\"ps\"]:.1f}}x' if p.get('ps') else '—'; \
        ev = f'{{p[\"ev_ebitda\"]:.1f}}x' if p.get('ev_ebitda') else '—'; \
        print(f'  {{p[\"ticker\"]:<8} {{pe:>6}} {{ps:>6}} {{ev:>8}}'); \
    med = pc.get('sector_medians', {}); \
    if med: print(f'  {{\"Median\":<8} {{(str(round(med[\"pe\"],1))+\"x\") if med.get(\"pe\") else \"—\":>6}} {{(str(round(med[\"ps\"],1))+\"x\") if med.get(\"ps\") else \"—\":>6}} {{(str(round(med[\"ev_ebitda\"],1))+\"x\") if med.get(\"ev_ebitda\") else \"—\":>8}}'); \
    rk = pc.get('percentile_ranks', {}); \
    spe = f'{{subj.get(\"pe\",0):.1f}}x' if subj.get('pe') else '—'; \
    sps = f'{{subj.get(\"ps\",0):.1f}}x' if subj.get('ps') else '—'; \
    sev = f'{{subj.get(\"ev_ebitda\",0):.1f}}x' if subj.get('ev_ebitda') else '—'; \
    print(f'  {{t+\" ★\":<8} {{spe:>6}} {{sps:>6}} {{sev:>8}}  (p{{rk.get(\"pe\",\"?\")}} p{{rk.get(\"ps\",\"?\")}} p{{rk.get(\"ev_ebitda\",\"?\")}} — lower = cheaper)'); \
else: \
    print('  Peer Comparables: No peer data available'); \
print(); \
"

# =============================================================================
# SCREENING & WATCHLISTS
# =============================================================================

WATCHLIST ?=
PRESET ?=
NAME ?=
ID ?=
RUN_ID ?=
TOP ?= 20
FORMAT ?= txt
OUT ?=
HORIZON ?= short
LH_HORIZON ?= long_6to12m
LH_USE_EXPANDED ?= true
N ?= 5
INCLUDE_LOSERS ?= true
WL_ID ?=
PREWARM_MAX ?= 600
PREWARM_SOURCES ?= built-in,user,auto
PREWARM_EXCLUDE_PREFIXES ?= Movers:
PREWARM_METADATA_WORKERS ?= 12
PREWARM_METADATA_MAX_AGE_DAYS ?= 7
PREWARM_INFO_WORKERS ?= 12
PREWARM_SKIP_INFO ?= false

watchlists:
	@$(PYTHON) scripts/list_watchlists.py

screen:
	@[ -n "$(WATCHLIST)" ] || (echo "WATCHLIST is required. Example: make screen WATCHLIST='AAPL MSFT NVDA' PRESET=momentum_hunter"; exit 1)
	@$(PYTHON) scripts/run_screen.py $(WATCHLIST) $(if $(PRESET),--preset $(PRESET)) --top $(TOP)

screen-watchlist:
	@if [ -n "$(NAME)" ]; then \
		$(PYTHON) scripts/run_screen.py --name "$(NAME)" $(if $(PRESET),--preset $(PRESET)) $(if $(MODE),--mode $(MODE)) --top $(TOP); \
	elif [ -n "$(ID)" ]; then \
		$(PYTHON) scripts/run_screen.py --id $(ID) $(if $(PRESET),--preset $(PRESET)) $(if $(MODE),--mode $(MODE)) --top $(TOP); \
	else \
		echo "NAME or ID is required."; \
		echo "  make screen-watchlist NAME=\"AI Infrastructure Metals\" PRESET=momentum_hunter"; \
		echo "  make screen-watchlist ID=5 PRESET=value_fisher"; \
		echo "  make screen-watchlist NAME=\"NASDAQ 100\" MODE=reversal_buildup"; \
		echo "  make screen-watchlist NAME=\"NASDAQ 100\" MODE=base_coil"; \
		echo ""; \
		echo "Run 'make watchlists' to see available watchlists."; \
		exit 1; \
	fi

screen-all:
	@echo "Scanning ALL watchlists (sequential, each uses its default preset)..."
	@$(PYTHON) scripts/run_screen_all.py --top $(TOP)

# Background scan via the jobs API. Safe for 3k+ ticker universes — the CLI
# returns immediately with a job_id. Poll progress with screen-all-status.
screen-all-async:
	@echo "Submitting scan-all to the jobs API$(if $(MODE), [MODE=$(MODE)],)..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/screen-all" \
		-H "Content-Type: application/json" \
		-d "{\"mode\": \"$(if $(MODE),$(MODE),standard)\"}" 2>/dev/null); \
	echo "$$resp" | $(PYTHON) -c "\
import sys, json; \
d = json.load(sys.stdin); \
jid = d.get('job_id'); \
print(f'  job_id: {jid}' if jid else f'  unexpected response: {d}'); \
print(f'  watchlists: {d.get(\"watchlists_queued\", \"?\")}  total_tickers: {d.get(\"total_tickers\", \"?\")}  mode: {d.get(\"mode\", \"?\")}'); \
print(f'  poll with: make screen-all-status ID={jid}' if jid else '')" \
		|| echo "FAILED — is the server running? Try 'make status'"

# Poll a scan-all job. Pass ID=<job_id>.
screen-all-status:
	@[ -n "$(ID)" ] || (echo "ID is required. Example: make screen-all-status ID=<job_id>"; exit 1)
	@curl -sS "http://localhost:$(PORT)/api/jobs/$(ID)" | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

# Follow a scan-all job until completion (2s poll). Pass ID=<job_id>.
screen-all-watch:
	@[ -n "$(ID)" ] || (echo "ID is required. Example: make screen-all-watch ID=<job_id>"; exit 1)
	@echo "Following job $(ID) (Ctrl+C to stop)..."
	@while :; do \
		status=$$(curl -sS "http://localhost:$(PORT)/api/jobs/$(ID)" 2>/dev/null | $(PYTHON) -c "import sys, json; d=json.load(sys.stdin); print('not_found' if d.get('detail') == 'Job not found' else d.get('status', 'unknown'))" 2>/dev/null); \
		printf '  %s\n' "$$(date '+%H:%M:%S') status=$$status"; \
		case "$$status" in completed|failed|cancelled|not_found) break ;; esac; \
		sleep 2; \
	done; \
	if [ "$$status" = "not_found" ]; then \
		echo "Job $(ID) not found (likely from an older server session). Start a new 'make screen-all-async' run."; \
	else \
		curl -sS "http://localhost:$(PORT)/api/jobs/$(ID)" | $(PYTHON) -m json.tool; \
	fi

# Warm the OHLCV cache for the entire union universe so the next real
# scan-all avoids 3k+ cold cache hits. Honors the YFinance limiter.
screen-all-prewarm:
	@echo "Prewarming OHLCV cache for scan-all union universe..."
	@$(PYTHON) scripts/prewarm_screen_all.py $(PREWARM_ARGS)

export-tradingview-latest:
	@fmt=$$(echo "$(FORMAT)" | tr '[:upper:]' '[:lower:]'); \
	if [ "$$fmt" != "txt" ] && [ "$$fmt" != "csv" ]; then \
		echo "FORMAT must be txt or csv"; \
		exit 1; \
	fi; \
	out="$(OUT)"; \
	if [ -z "$$out" ]; then \
		out="scan_all_top_$(TOP)_tradingview.$$fmt"; \
	fi; \
	echo "Exporting latest scan-all top $(TOP) to $$out..."; \
	curl -sf "http://localhost:$(PORT)/api/screen-all/latest/export/tradingview?top=$(TOP)&format=$$fmt" -o "$$out"; \
	echo "Saved $$out"

export-tradingview-run:
	@[ -n "$(RUN_ID)" ] || (echo "RUN_ID is required. Example: make export-tradingview-run RUN_ID=123"; exit 1)
	@fmt=$$(echo "$(FORMAT)" | tr '[:upper:]' '[:lower:]'); \
	if [ "$$fmt" != "txt" ] && [ "$$fmt" != "csv" ]; then \
		echo "FORMAT must be txt or csv"; \
		exit 1; \
	fi; \
	out="$(OUT)"; \
	if [ -z "$$out" ]; then \
		out="run_$(RUN_ID)_top_$(TOP)_tradingview.$$fmt"; \
	fi; \
	echo "Exporting run $(RUN_ID) top $(TOP) to $$out..."; \
	curl -sf "http://localhost:$(PORT)/api/screen/runs/$(RUN_ID)/export/tradingview?top=$(TOP)&format=$$fmt" -o "$$out"; \
	echo "Saved $$out"

screen-backtest:
	@[ -n "$(RUN_ID)" ] || (echo "RUN_ID is required. Example: make screen-backtest RUN_ID=1"; exit 1)
	@$(PYTHON) scripts/run_screen_backtest.py "$(RUN_ID)"

movers-scan:
	@echo "Running Movers Intelligence scan (top=$(TOP), include_losers=$(INCLUDE_LOSERS))..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/movers/scan" \
		-H "Content-Type: application/json" \
		-d "{\"top_n\": $(TOP), \"include_losers\": $(INCLUDE_LOSERS)}"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

movers-queue-top:
	@echo "Queueing latest movers top $(N) (horizon=$(HORIZON))..."
	@run_json=$$(curl -sS "http://localhost:$(PORT)/api/movers/runs/latest?horizon=$(HORIZON)"); \
	run_id=$$(printf "%s" "$$run_json" | $(PYTHON) -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null); \
	if [ -z "$$run_id" ]; then echo "$$run_json" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$run_json"; exit 1; fi; \
	resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/movers/runs/$$run_id/queue-top?top_n=$(N)&mode=$(MODE)&risk_profile=$(RISK)"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

movers-alerts:
	@echo "Creating movers alerts from latest run (horizon=$(HORIZON), top=$(TOP))..."
	@run_json=$$(curl -sS "http://localhost:$(PORT)/api/movers/runs/latest?horizon=$(HORIZON)"); \
	run_id=$$(printf "%s" "$$run_json" | $(PYTHON) -c "import sys,json; d=json.load(sys.stdin); print(d.get('id',''))" 2>/dev/null); \
	if [ -z "$$run_id" ]; then echo "$$run_json" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$run_json"; exit 1; fi; \
	resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/movers/runs/$$run_id/alerts?top_n=$(TOP)"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

long-horizon-scan:
	@use_expanded=$$(echo "$(LH_USE_EXPANDED)" | tr '[:upper:]' '[:lower:]'); \
	expanded_enabled=0; \
	if [ "$$use_expanded" = "1" ] || [ "$$use_expanded" = "true" ] || [ "$$use_expanded" = "yes" ] || [ "$$use_expanded" = "on" ]; then expanded_enabled=1; fi; \
	if [ "$$expanded_enabled" = "0" ] && [ -z "$(WL_ID)" ] && [ -z "$(TICKERS)" ]; then \
		echo "Provide WL_ID or TICKERS when LH_USE_EXPANDED is false. Example: make long-horizon-scan LH_USE_EXPANDED=false TICKERS=\"AAPL MSFT NVDA\""; \
		exit 1; \
	fi; \
	echo "Running long-horizon scan (horizon=$(LH_HORIZON), top=$(TOP), expanded=$$expanded_enabled)..."; \
	body=$$($(PYTHON) -c "import json; enabled='$(LH_USE_EXPANDED)'.strip().lower() in {'1','true','yes','on'}; print(json.dumps({'horizon':'$(LH_HORIZON)','top_n':int('$(TOP)'),'use_expanded_universe':enabled}))"); \
	if [ -n "$(WL_ID)" ]; then body=$$(printf '%s' "$$body" | $(PYTHON) -c "import json,sys; x=json.loads(sys.stdin.read()); x['watchlist_id']=int('$(WL_ID)'); print(json.dumps(x))"); fi; \
	if [ -n "$(TICKERS)" ]; then body=$$(printf '%s' "$$body" | $(PYTHON) -c "import json,sys; x=json.loads(sys.stdin.read()); x['tickers']=[t.strip().upper() for t in '$(TICKERS)'.split() if t.strip()]; print(json.dumps(x))"); fi; \
	resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/long-horizon/scan" -H "Content-Type: application/json" -d "$$body"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

long-horizon-prewarm:
	@echo "Prewarming long-horizon expanded-universe caches..."
	@args="--db-path $(DB_PATH) --max-tickers $(PREWARM_MAX) --sources $(PREWARM_SOURCES) --exclude-prefixes $(PREWARM_EXCLUDE_PREFIXES) --metadata-workers $(PREWARM_METADATA_WORKERS) --metadata-max-age-days $(PREWARM_METADATA_MAX_AGE_DAYS) --info-workers $(PREWARM_INFO_WORKERS)"; \
	skip=$$(echo "$(PREWARM_SKIP_INFO)" | tr '[:upper:]' '[:lower:]'); \
	if [ "$$skip" = "1" ] || [ "$$skip" = "true" ] || [ "$$skip" = "yes" ] || [ "$$skip" = "on" ]; then args="$$args --skip-info"; fi; \
	$(PYTHON) scripts/prewarm_long_horizon.py $$args

long-horizon-status:
	@echo "Fetching long-horizon status..."
	@curl -sS "http://localhost:$(PORT)/api/long-horizon/status" | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

long-horizon-runs:
	@echo "Listing long-horizon runs..."
	@curl -sS "http://localhost:$(PORT)/api/long-horizon/runs?limit=20&offset=0" | $(PYTHON) -m json.tool 2>/dev/null \
		|| echo "FAILED — is the server running? Try 'make status'"

long-horizon-underwrite:
	@[ -n "$(RUN_ID)" ] || (echo "RUN_ID is required. Example: make long-horizon-underwrite RUN_ID=123 TOP=10"; exit 1)
	@echo "Underwriting long-horizon run $(RUN_ID), top=$(TOP)..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/long-horizon/runs/$(RUN_ID)/underwrite-top" \
		-H "Content-Type: application/json" \
		-d "{\"top_n\": $(TOP)}"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

long-horizon-allocate:
	@[ -n "$(RUN_ID)" ] || (echo "RUN_ID is required. Example: make long-horizon-allocate RUN_ID=123"; exit 1)
	@echo "Allocating long-horizon run $(RUN_ID)..."
	@resp=$$(curl -sS -X POST "http://localhost:$(PORT)/api/long-horizon/runs/$(RUN_ID)/allocate" \
		-H "Content-Type: application/json" \
		-d "{\"policy_name\": \"default\"}"); \
	echo "$$resp" | $(PYTHON) -m json.tool 2>/dev/null || echo "$$resp"

# Portfolio risk summary for a completed long-horizon run (requires server).
portfolio-risk:
	@[ -n "$(RUN_ID)" ] || (echo "RUN_ID is required. Example: make portfolio-risk RUN_ID=123"; exit 1)
	@$(PYTHON) -c "\
import urllib.request, json; \
url = 'http://localhost:$(PORT)/api/long-horizon/runs/$(RUN_ID)'; \
try: \
    with urllib.request.urlopen(url, timeout=10) as r: detail = json.loads(r.read()); \
except Exception as e: print(f'FAILED: {e}. Is the server running? Try make status'); exit(1); \
risk = (detail.get('meta') or {}).get('portfolio_risk', {}); \
if not risk: print('  No portfolio_risk in run $(RUN_ID) meta. Re-run long-horizon-scan to generate it.'); exit(0); \
print(); \
print('  Portfolio Risk Summary — Run #$(RUN_ID)'); \
print('  ' + '='*44); \
beta = risk.get('weighted_beta'); \
hhi  = risk.get('sector_hhi'); \
top  = risk.get('top_factor','—'); \
n    = risk.get('ticker_count','?'); \
print(f'  Tickers:        {n}'); \
print(f'  Weighted Beta:  {beta:.3f}' if beta is not None else '  Weighted Beta:  —'); \
conc = 'Concentrated' if hhi and hhi > 0.25 else ('Moderate' if hhi and hhi > 0.15 else 'Diversified'); \
print(f'  Sector HHI:     {hhi:.3f}  ({conc})' if hhi is not None else '  Sector HHI:     —'); \
print(f'  Top Factor:     {top}'); \
print(); \
sc = risk.get('sector_concentration', {}); \
if sc: \
    print('  Sector Concentration:'); \
    for sec, pct in sorted(sc.items(), key=lambda x: -x[1]): print(f'    {sec:<22} {pct*100:.1f}%'); \
    print(); \
fe = risk.get('factor_exposures', {}); \
if fe: \
    print('  Factor Exposures (portfolio weighted avg):'); \
    for fam, score in sorted(fe.items(), key=lambda x: -x[1]): \
        bar = '█' * int(score/10) + '░' * (10 - int(score/10)); \
        print(f'    {fam:<12} {bar}  {score:.0f}/100'); \
print(); \
"

# =============================================================================
# INVESTMENT PROFILES
# =============================================================================

profiles:
	@echo ""
	@echo "Available Investment Profiles"
	@echo "============================="
	@echo ""
	@echo "  large_cap_core        Large Cap Core (default for stable blue-chips)"
	@echo "                        Focus: valuation, margins, FCF yield, dividend sustainability"
	@echo ""
	@echo "  dividend_income       Dividend & Income"
	@echo "                        Focus: yield stability, payout ratio, cash flow coverage"
	@echo ""
	@echo "  high_growth           High Growth"
	@echo "                        Focus: revenue acceleration, TAM, earnings beats, momentum"
	@echo ""
	@echo "  commodity_cyclical    Commodity / Cyclical"
	@echo "                        Focus: supply/demand, macro cycle, commodity prices, capex"
	@echo ""
	@echo "  momentum_speculative  Momentum / Speculative"
	@echo "                        Focus: volatility, short interest, catalysts, technical breakouts"
	@echo ""
	@echo "Note: Profiles are auto-resolved per ticker via metadata tagging."
	@echo "      You only need to specify a profile if you want to override the auto-assignment."
	@echo ""
	@echo "Usage:"
	@echo "  make analyze TICKER=NVDA PROFILE=high_growth"
	@echo "  make batch-analyze TICKERS=\"XOM CVX\" DATE=2026-01-27 PROFILE=commodity_cyclical"
	@echo ""

# =============================================================================
# BACKTESTING
# =============================================================================

backtest:
	$(PYTHON) scripts/run_backtest.py $(BACKTEST_ARGS)

backtest-report:
	$(PYTHON) scripts/generate_backtest_run_report.py $(BACKTEST_REPORT_ARGS)

calibration-report:
	$(PYTHON) scripts/generate_calibration_report.py $(CALIBRATION_ARGS)

# Calibration sliced by model decision (BUY / HOLD / SELL) — detects decision-class confidence bias.
calibration-by-decision:
	@$(PYTHON) -c "\
from tradingagents.backtesting.calibration import compute_calibration_by_decision; \
import json; \
data = compute_calibration_by_decision(); \
print(); \
print('  Calibration by Decision (BUY / HOLD / SELL)'); \
print('  ' + '='*44); \
for decision, d in data.items(): \
    meta = d.get('meta', {}); \
    n = meta.get('total_samples', 0); \
    if n == 0: print(f'  {decision:<8}  — no data'); continue; \
    bins = d.get('bins', []); \
    valid = [(c, a) for c, a, cnt in bins if c >= 0 and a >= 0]; \
    if not valid: print(f'  {decision:<8}  — insufficient data'); continue; \
    avg_gap = sum(a*100 - c for c, a in valid) / len(valid); \
    lbl = 'overconfident' if avg_gap < -3 else ('underconfident' if avg_gap > 3 else 'well-calibrated'); \
    print(f'  {decision:<8}  {n} samples  avg gap {avg_gap:+.1f}pp  → {lbl}'); \
print(); \
"

# Calibration sliced by dominant factor family (Value / Quality / Momentum / etc.).
calibration-by-factor:
	@$(PYTHON) -c "\
from tradingagents.backtesting.calibration import compute_calibration_by_factor; \
import json; \
data = compute_calibration_by_factor(); \
print(); \
print('  Calibration by Factor Family'); \
print('  ' + '='*44); \
for factor, d in data.items(): \
    meta = d.get('meta', {}); \
    n = meta.get('total_samples', 0); \
    if n == 0: print(f'  {factor:<14}  — no data'); continue; \
    bins = d.get('bins', []); \
    valid = [(c, a) for c, a, cnt in bins if c >= 0 and a >= 0]; \
    if not valid: print(f'  {factor:<14}  — insufficient data'); continue; \
    avg_gap = sum(a*100 - c for c, a in valid) / len(valid); \
    lbl = 'overconfident' if avg_gap < -3 else ('underconfident' if avg_gap > 3 else 'calibrated'); \
    print(f'  {factor:<14}  {n} samples  avg gap {avg_gap:+.1f}pp  → {lbl}'); \
print(); \
"

# Reverse-DCF accuracy: implied growth vs analyst consensus across all stored analyses.
reverse-dcf-accuracy:
	@$(PYTHON) -c "\
import json, statistics; \
from tradingagents.reporting.database import get_db; \
db = get_db('$(DB_PATH)'); \
analyses = db.get_backtested_analyses(limit=200); \
errors = []; \
rows = []; \
for a in analyses: \
    try: \
        import json as _j; \
        iv = _j.loads(getattr(a, 'intrinsic_value_data', '') or '{}'); \
        implied = iv.get('implied_growth_rate'); \
        if implied is None: continue; \
        from tradingagents.dataflows.yfinance_extended import get_ticker_info; \
        info = get_ticker_info(a.ticker); \
        consensus = info.get('earningsGrowth') or info.get('revenueGrowth'); \
        if consensus is None: continue; \
        err = abs(float(implied) - float(consensus)); \
        errors.append(err); \
        rows.append((a.ticker, a.analysis_date, float(implied)*100, float(consensus)*100, err*100)); \
    except: pass; \
print(); \
print('  Reverse-DCF Accuracy'); \
print('  ' + '='*44); \
if not errors: print('  No data yet. Run analyses to populate implied_growth_rate.'); \
else: \
    print(f'  Analyses with data:  {len(errors)}'); \
    print(f'  Mean abs error:      {statistics.mean(errors)*100:.1f}pp'); \
    print(f'  Median abs error:    {statistics.median(errors)*100:.1f}pp'); \
    print(); \
    print(f'  {\"Ticker\":<8} {\"Date\":<12} {\"Implied\":>8} {\"Consensus\":>10} {\"Error\":>7}'); \
    print('  ' + '-'*48); \
    for r in sorted(rows, key=lambda x: x[4])[:20]: \
        print(f'  {r[0]:<8} {str(r[1])[:10]:<12} {r[2]:>7.1f}%  {r[3]:>9.1f}%  {r[4]:>6.1f}pp'); \
print(); \
"

# Run all calibration analyses in sequence.
calibration-all: calibration-report calibration-by-decision calibration-by-factor reverse-dcf-accuracy

# =============================================================================
# ALERTS
# =============================================================================

alerts:
	@$(PYTHON) scripts/show_alerts.py

macro-status:
	@$(PYTHON) -c "\
from tradingagents.dataflows.yfinance_extended import get_macro_snapshot; \
import json; \
m = get_macro_snapshot(); \
print(); \
print('  Macro Environment Status'); \
print('  ========================'); \
print(f'  Market Regime:   {m.get(\"market_regime\", \"unknown\")}'); \
print(f'  VIX Level:       {m.get(\"vix_level\", \"unknown\")}'); \
print(f'  Yield Curve:     {m.get(\"yield_curve\", \"unknown\")}'); \
print(f'  Dollar Trend:    {m.get(\"dollar_trend\", \"unknown\")}'); \
print(f'  Credit Stress:   {m.get(\"credit_stress\", \"unknown\")}'); \
print(f'  Sector Breadth:  {m.get(\"sector_breadth\", \"unknown\")} ({m.get(\"sectors_above_sma50\", 0)}/{m.get(\"sectors_total\", 0)} above SMA50)'); \
print(f'  Fetched:         {m.get(\"fetched_at\", \"\")}'); \
print(); \
"

# =============================================================================
# QA & TESTING
# =============================================================================

qa: qa-report qa-report-tests

qa-report:
	$(PYTHON) scripts/report_fast_qa.py

qa-report-update:
	$(PYTHON) scripts/report_fast_qa.py --update-goldens

qa-report-tests:
	@if command -v pytest >/dev/null 2>&1; then \
		pytest tests/ -q; \
	else \
		echo "pytest not installed; falling back to unittest discover"; \
		$(PYTHON) -m unittest discover -s tests -p "test_*.py"; \
	fi

qa-manual:
	@echo "Manual QA scripts (run individually as needed):"
	@echo "  $(PYTHON) scripts/qa/test_qa_fixes.py"
	@echo "  $(PYTHON) scripts/qa/simulate_ui_rendering.py"
	@echo "  bash scripts/qa/quick_check_screener.sh"

test: qa-report-tests

# End-to-end smoke test of the scan-all pipeline on a small universe.
# Honors SMOKE_BUDGET_SEC (default 180s).
qa-screen-all-smoke:
	@echo "Running scan-all smoke test (Sector ETFs + NASDAQ 100)..."
	@$(PYTHON) scripts/qa/smoke_screen_all.py

qa-healthcare-watchlist-smoke:
	@echo "Running Healthcare & Life Sciences scan smoke (budget 300s)..."
	@SMOKE_WATCHLISTS="Healthcare & Life Sciences" SMOKE_BUDGET_SEC=300 $(PYTHON) scripts/qa/smoke_screen_all.py

stabilization-gate:
	@echo "Running stabilization gate tests (refresh + movers + long-horizon + universe + limiter + hardening waves + institutional parity)..."
	@pytest tests/test_builtin_refresh_hardening.py \
		tests/test_builtin_refresh_apply_gate.py \
		tests/test_builtin_refresh_tiered_validation.py \
		tests/test_movers_intelligence.py \
		tests/test_movers_lineage.py \
		tests/test_movers_lineage_per_horizon.py \
		tests/test_movers_guards.py \
		tests/test_movers_scheduler_lock.py \
		tests/test_long_horizon_workflow.py \
		tests/test_tradingview_exports.py \
		tests/test_watchlist_universe_restructure.py \
		tests/test_yfinance_limiter.py \
		tests/test_wave1_hardening.py \
		tests/test_wave2_hardening.py \
		tests/test_wave3_hardening.py \
		tests/test_signal_pipeline.py \
		tests/test_screening_signals.py \
		tests/test_ranking_refinements.py \
		tests/test_calibration.py \
		tests/test_institutional_parity.py

lint:
	@if command -v ruff >/dev/null 2>&1; then \
		echo "Running ruff..."; \
		ruff check tradingagents/ webapp/ cli/ scripts/ tests/ --select E,F,W --ignore E501; \
	else \
		echo "Running python -m py_compile on key modules..."; \
		$(PYTHON) -m py_compile tradingagents/research.py && echo "  research.py OK"; \
		$(PYTHON) -m py_compile webapp/app.py && echo "  app.py OK"; \
		$(PYTHON) -m py_compile tradingagents/screening/engine.py && echo "  engine.py OK"; \
		$(PYTHON) -m py_compile tradingagents/screening/macro_overlay.py && echo "  macro_overlay.py OK"; \
		$(PYTHON) -m py_compile tradingagents/screening/ticker_resolver.py && echo "  ticker_resolver.py OK"; \
		$(PYTHON) -m py_compile tradingagents/reporting/database.py && echo "  database.py OK"; \
		$(PYTHON) -m py_compile tradingagents/reporting/pdf_generator.py && echo "  pdf_generator.py OK"; \
		$(PYTHON) -m py_compile tradingagents/graph/trading_graph.py && echo "  trading_graph.py OK"; \
		$(PYTHON) -m py_compile tradingagents/graph/signal_aggregator.py && echo "  signal_aggregator.py OK"; \
		$(PYTHON) -m py_compile tradingagents/graph/signal_processing.py && echo "  signal_processing.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/cache.py && echo "  cache.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/yfinance_extended.py && echo "  yfinance_extended.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/y_finance.py && echo "  y_finance.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/perplexity_api.py && echo "  perplexity_api.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/finnhub_api.py && echo "  finnhub_api.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/yfinance_limiter.py && echo "  yfinance_limiter.py OK"; \
		$(PYTHON) -m py_compile tradingagents/screening/alert_evaluator.py && echo "  alert_evaluator.py OK"; \
		$(PYTHON) -m py_compile tradingagents/screening/long_horizon.py && echo "  long_horizon.py OK"; \
		$(PYTHON) -m py_compile webapp/alert_monitor.py && echo "  alert_monitor.py OK"; \
		$(PYTHON) -m py_compile tradingagents/default_config.py && echo "  default_config.py OK"; \
		$(PYTHON) -m py_compile tradingagents/utils/token_management.py && echo "  token_management.py OK"; \
		$(PYTHON) -m py_compile tradingagents/utils/config_validation.py && echo "  config_validation.py OK"; \
		$(PYTHON) -m py_compile tradingagents/utils/logging_config.py && echo "  logging_config.py OK"; \
		$(PYTHON) -m py_compile tradingagents/utils/llm_usage.py && echo "  llm_usage.py OK"; \
		$(PYTHON) -m py_compile tradingagents/utils/snapshot_utils.py && echo "  snapshot_utils.py OK"; \
		$(PYTHON) -m py_compile tradingagents/research.py && echo "  research.py OK"; \
		$(PYTHON) -m py_compile tradingagents/backtesting/calibration.py && echo "  backtesting/calibration.py OK"; \
		$(PYTHON) -m py_compile tradingagents/backtesting/engine.py && echo "  backtesting/engine.py OK"; \
		$(PYTHON) -m py_compile tradingagents/reporting/attribution.py && echo "  reporting/attribution.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/provenance.py && echo "  dataflows/provenance.py OK"; \
		$(PYTHON) -m py_compile tradingagents/dataflows/risk_metrics.py && echo "  dataflows/risk_metrics.py OK"; \
		echo "Tip: install ruff for full linting: pip install ruff"; \
	fi

# =============================================================================
# REPORTS & COMPARISONS
# =============================================================================

A ?=
B ?=

diff:
	@[ -n "$(TICKER)" ] || (echo "TICKER is required. Example: make diff TICKER=NVDA A=2026-01-01 B=2026-02-01"; exit 1)
	@[ -n "$(A)" ] || (echo "A (date) is required. Example: make diff TICKER=NVDA A=2026-01-01 B=2026-02-01"; exit 1)
	@[ -n "$(B)" ] || (echo "B (date) is required. Example: make diff TICKER=NVDA A=2026-01-01 B=2026-02-01"; exit 1)
	$(PYTHON) scripts/report_diff.py --ticker $(TICKER) --date-a $(A) --date-b $(B)

# =============================================================================
# DATABASE
# =============================================================================

DB_PATH ?= research.db

db-info:
	@echo "Database: $(DB_PATH)"
	@echo "---"
	@if [ -f "$(DB_PATH)" ]; then \
		echo "Size: $$(du -h "$(DB_PATH)" | cut -f1)"; \
		echo ""; \
		echo "Table               Rows"; \
		echo "------------------- --------"; \
		for table in analyses watchlists screening_runs screening_results movers_snapshots movers_run_details long_horizon_run_meta long_horizon_run_details long_horizon_underwriting alert_rules alert_history backtest_runs ticker_metadata ticker_exchange_resolution index_constituents runtime_metrics builtin_refresh_proposals builtin_refresh_items signal_performance snapshot_kpis snapshot_guidance snapshot_risks event_calendar; do \
			count=$$(sqlite3 "$(DB_PATH)" "SELECT COUNT(*) FROM $$table;" 2>/dev/null || echo "—"); \
			printf "%-20s %s\n" "$$table" "$$count"; \
		done; \
	else \
		echo "Database file not found. It will be created on first run."; \
	fi

db-backup:
	@if [ -f "$(DB_PATH)" ]; then \
		mkdir -p backups; \
		BACKUP="backups/$(DB_PATH).backup_$$(date +%Y%m%d_%H%M%S)"; \
		cp "$(DB_PATH)" "$$BACKUP"; \
		echo "Backed up to $$BACKUP ($$(du -h "$$BACKUP" | cut -f1))"; \
		$(PYTHON) -c "\
from tradingagents.utils.cleanup_manager import CleanupManager; \
cm = CleanupManager(db_path='$(DB_PATH)'); \
r = cm.cleanup_db_backups(max_keep=$(KEEP), dry_run=False); \
removed = r['count']; \
kept = r['kept']; \
total = r['total_backups']; \
print(f'  Backups: {kept} kept of {total} (removed {removed} old)') if total > 0 else None; \
"; \
	else \
		echo "No database to back up."; \
	fi

db-reset:
	@echo "WARNING: This will permanently delete $(DB_PATH) and all data."
	@read -p "Type 'yes' to confirm: " confirm && [ "$$confirm" = "yes" ] || (echo "Cancelled."; exit 1)
	@rm -f "$(DB_PATH)"
	@echo "Database deleted. A fresh one will be created on next startup."

# =============================================================================
# CLEANUP
# =============================================================================

clean: clean-cache

clean-cache:
	@echo "Clearing API cache..."
	@rm -rf tradingagents/dataflows/data_cache/*.json tradingagents/dataflows/data_cache/*.csv 2>/dev/null || true
	@echo "Cache cleared"

TYPE ?=
clean-cache-type:
	@[ -n "$(TYPE)" ] || (echo "TYPE is required. Example: make clean-cache-type TYPE=estimate_revisions"; echo "Types: estimate_revisions, rating_changes, earnings_quality, intrinsic_value, catalyst_pipeline,"; echo "       weekly_technicals, sector_breadth, macro_snapshot, ticker_info,"; echo "       peer_comps, corporate_actions, insider_net_buy"; exit 1)
	@echo "Clearing cache entries matching '$(TYPE)'..."
	@count=$$(ls tradingagents/dataflows/data_cache/*$(TYPE)* 2>/dev/null | wc -l | tr -d ' '); \
	rm -f tradingagents/dataflows/data_cache/*$(TYPE)* 2>/dev/null || true; \
	echo "  Removed $$count file(s)"

clean-reports:
	@echo "This will delete ALL generated reports in research_output/ (including subdirectories)."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 0
	@rm -f research_output/*.html research_output/*.pdf research_output/*.csv research_output/*.json research_output/*.md 2>/dev/null || true
	@rm -rf research_output/reports/ research_output/summaries/ research_output/briefs/ research_output/snapshots/ research_output/exports/ research_output/calibration/ 2>/dev/null || true
	@echo "Reports cleared"

clean-logs:
	@echo "Clearing log files..."
	@rm -f logs/*.log logs/*.log.* 2>/dev/null || true
	@echo "Logs cleared"

clean-eval:
	@echo "This will delete ALL eval_results/ state logs."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 0
	@rm -rf eval_results/* 2>/dev/null || true
	@echo "Eval results cleared"

clean-results:
	@echo "This will delete ALL legacy results/ output."
	@read -p "Are you sure? [y/N] " confirm && [ "$$confirm" = "y" ] || exit 0
	@rm -rf results/* 2>/dev/null || true
	@echo "Legacy results cleared"

DAYS ?= 90
clean-old:
	@echo "Running age-based cleanup (max_age=$(DAYS) days)..."
	@$(PYTHON) -c "\
from tradingagents.utils.cleanup_manager import CleanupManager; \
cm = CleanupManager(); \
r = cm.cleanup_all(max_age_days=$(DAYS), dry_run=False); \
rpt = r['reports']; \
ev = r['eval_results']; \
csv = r['cache_csvs']; \
bk = r['db_backups']; \
movers = r.get('movers', {}); \
print(f'  Reports removed:      {rpt[\"count\"]}'); \
print(f'  Eval logs removed:    {ev[\"count\"]}'); \
print(f'  Cache CSVs removed:   {csv[\"count\"]}'); \
print(f'  DB backups removed:   {bk[\"count\"]} (kept {bk[\"kept\"]})');\
print(f'  Movers snapshots:     {movers.get(\"movers_snapshots\", 0)}'); \
print(f'  Movers run details:   {movers.get(\"movers_run_details\", 0)}');\
"

clean-auto-watchlists:
	@if [ "$(FORCE)" != "1" ]; then \
		echo "This will delete ALL source=auto watchlists and their screening history."; \
		read -p "Proceed? [y/N] " confirm; \
		[ "$$confirm" = "y" ] || exit 0; \
	fi
	@$(PYTHON) -c "from tradingagents.reporting.database import get_db; db=get_db('$(DB_PATH)'); auto=db.get_auto_watchlists(); deleted=sum(1 for w in auto if db.delete_watchlist(int(w['id']))); print(f'Deleted {deleted} auto watchlist(s).')"

storage-report:
	@$(PYTHON) -c "$$STORAGE_REPORT_SCRIPT"

define STORAGE_REPORT_SCRIPT
from tradingagents.utils.cleanup_manager import CleanupManager
cm = CleanupManager()
r = cm.get_storage_report()
print()
print("  Storage Report")
print("  ==============")
for k, v in r.items():
    if k == "total":
        continue
    fc = v.get("file_count", "-")
    mb = v.get("size_mb", 0)
    print(f"  {k:<20s}  {fc:>6} files   {mb:>8.2f} MB")
print(f"  {'─'*46}")
print(f"  {'TOTAL':<20s}  {'':>6}         {r['total']['size_mb']:>8.2f} MB")
print()
endef
export STORAGE_REPORT_SCRIPT

KEEP ?= 5
db-vacuum:
	@echo "Running VACUUM + ANALYZE on $(DB_PATH)..."
	@$(PYTHON) -c "\
from tradingagents.reporting.database import get_db; \
db = get_db('$(DB_PATH)'); \
r = db.vacuum(); \
saved_kb = r['bytes_saved'] / 1024; \
print(f'  Before: {r[\"size_before\"] / (1024*1024):.2f} MB'); \
print(f'  After:  {r[\"size_after\"] / (1024*1024):.2f} MB'); \
print(f'  Saved:  {saved_kb:.1f} KB'); \
"

db-cleanup-backups:
	@echo "Pruning old database backups (keeping $(KEEP) most recent)..."
	@$(PYTHON) -c "\
from tradingagents.utils.cleanup_manager import CleanupManager; \
cm = CleanupManager(db_path='$(DB_PATH)'); \
r = cm.cleanup_db_backups(max_keep=$(KEEP), dry_run=False); \
print(f'  Total backups: {r[\"total_backups\"]}'); \
print(f'  Kept: {r[\"kept\"]}'); \
print(f'  Removed: {r[\"count\"]}'); \
"

clean-all: clean-cache clean-logs
	@if [ "$(FORCE)" != "1" ]; then \
		echo "This will remove eval logs, prune DB backups, and clear Python caches."; \
		read -p "Proceed with clean-all? [y/N] " confirm; \
		[ "$$confirm" = "y" ] || exit 0; \
	fi
	@echo "Cleaning eval_results/..."
	@rm -rf eval_results/* 2>/dev/null || true
	@echo "Pruning old database backups..."
	@$(PYTHON) -c "\
from tradingagents.utils.cleanup_manager import CleanupManager; \
cm = CleanupManager(); \
cm.cleanup_db_backups(max_keep=3, dry_run=False); \
" 2>/dev/null || true
	@echo "Removing __pycache__ directories..."
	@find . -type d -name __pycache__ -not -path './.venv/*' -not -path './archive/*' -exec rm -rf {} + 2>/dev/null || true
	@echo "Removing .pyc files..."
	@find . -name '*.pyc' -not -path './.venv/*' -not -path './archive/*' -delete 2>/dev/null || true
	@echo "All cleaned"
