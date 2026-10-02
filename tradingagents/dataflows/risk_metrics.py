"""
Quantitative Risk Metrics — computed from yfinance price data.

Provides: Beta, Sharpe Ratio, Sortino Ratio, Max Drawdown,
Annualized Volatility, Value at Risk (95%), and Correlation to SPY.

All computation is local (numpy/pandas), no external API calls.
Placed in dataflows/ because it's consumed by reports, risk manager prompts,
and the screening engine — avoids circular dependency with screening/.
"""

import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

import logging

from .cache import get_cache, CacheConfig

logger = logging.getLogger("tradingagents.dataflows.risk_metrics")


def compute_risk_metrics(
    ticker: str,
    benchmark: str = "SPY",
    period: int = 252,
) -> Dict[str, Any]:
    """
    Compute quantitative risk metrics for a ticker.
    
    Args:
        ticker: Stock ticker symbol
        benchmark: Benchmark ticker for beta/correlation (default: SPY)
        period: Number of trading days to use (default: 252 = ~1 year)
        
    Returns:
        Dict with beta, sharpe, sortino, max_drawdown, volatility, var, correlation
    """
    cache = get_cache()
    cache_key = f"{ticker}_{benchmark}_{period}"
    cached = cache.get("risk_metrics", cache_key)
    if cached is not None:
        return cached
    
    try:
        # Fetch price data for ticker and benchmark
        end_date = datetime.now()
        # Fetch extra days to account for weekends/holidays
        start_date = end_date - timedelta(days=int(period * 1.6))
        
        data = yf.download(
            f"{ticker.upper()} {benchmark.upper()}",
            start=start_date.strftime("%Y-%m-%d"),
            end=end_date.strftime("%Y-%m-%d"),
            group_by="ticker",
            progress=False,
            auto_adjust=True,
        )
        
        if data.empty:
            return _empty_risk_metrics(ticker, "No price data available")
        
        # Extract close prices
        try:
            stock_close = data[ticker.upper()]["Close"].dropna()
            bench_close = data[benchmark.upper()]["Close"].dropna()
        except KeyError:
            return _empty_risk_metrics(ticker, f"Ticker {ticker} or {benchmark} not found in data")
        
        # Align dates
        aligned = pd.DataFrame({
            "stock": stock_close,
            "bench": bench_close,
        }).dropna()
        
        if len(aligned) < 30:
            return _empty_risk_metrics(ticker, f"Insufficient data: {len(aligned)} days")
        
        # Trim to requested period
        aligned = aligned.tail(period)
        
        # Daily returns
        stock_returns = aligned["stock"].pct_change().dropna()
        bench_returns = aligned["bench"].pct_change().dropna()
        
        if len(stock_returns) < 20:
            return _empty_risk_metrics(ticker, "Insufficient return data")
        
        risk_free_daily = 0.0
        try:
            from tradingagents.dataflows.yfinance_extended import get_macro_snapshot
            macro = get_macro_snapshot()
            treasury_3m = macro.get("indices", {}).get("treasury_3m", {}).get("current")
            if treasury_3m is not None:
                risk_free_daily = float(treasury_3m) / 100 / 252
        except Exception:
            pass
        
        # ===== BETA =====
        cov_matrix = np.cov(stock_returns, bench_returns)
        beta = float(cov_matrix[0, 1] / cov_matrix[1, 1]) if cov_matrix[1, 1] != 0 else None
        
        # ===== ANNUALIZED RETURN =====
        annualized_return = float(stock_returns.mean() * 252)
        
        # ===== ANNUALIZED VOLATILITY =====
        volatility = float(stock_returns.std() * np.sqrt(252))
        
        # ===== SHARPE RATIO =====
        risk_free_annual = risk_free_daily * 252
        sharpe = None
        if volatility > 0:
            sharpe = float((annualized_return - risk_free_annual) / volatility)
        
        # ===== SORTINO RATIO =====
        downside_returns = stock_returns[stock_returns < 0]
        downside_std = float(downside_returns.std() * np.sqrt(252)) if len(downside_returns) > 0 else 0
        sortino = None
        if downside_std > 0:
            sortino = float((annualized_return - risk_free_annual) / downside_std)
        
        # ===== MAX DRAWDOWN =====
        cumulative = (1 + stock_returns).cumprod()
        peak = cumulative.cummax()
        with np.errstate(divide="ignore", invalid="ignore"):
            drawdown = np.where(peak > 0, (cumulative - peak) / peak, 0.0)
        drawdown = np.nan_to_num(drawdown, nan=0.0, posinf=0.0, neginf=0.0)
        max_drawdown = float(np.min(drawdown))
        
        # ===== VALUE AT RISK (95%) =====
        var_95 = float(np.percentile(stock_returns, 5))

        # ===== CVaR (EXPECTED SHORTFALL, 95%) =====
        var_threshold = np.percentile(stock_returns, 5)
        tail_returns = stock_returns[stock_returns <= var_threshold]
        cvar_95 = float(tail_returns.mean()) if len(tail_returns) > 0 else var_95

        # ===== CORRELATION TO BENCHMARK =====
        corr_val = stock_returns.corr(bench_returns)
        correlation = float(corr_val) if (corr_val is not None and not pd.isna(corr_val)) else None
        
        result = {
            "ticker": ticker.upper(),
            "benchmark": benchmark.upper(),
            "period_days": len(stock_returns),
            "beta": round(beta, 3) if beta is not None else None,
            "annualized_return_pct": round(annualized_return * 100, 2),
            "sharpe_ratio": round(sharpe, 3) if sharpe is not None else None,
            "sortino_ratio": round(sortino, 3) if sortino is not None else None,
            "max_drawdown_pct": round(max_drawdown * 100, 2),
            "volatility_annual_pct": round(volatility * 100, 2),
            "var_95_pct": round(var_95 * 100, 3),
            "cvar_95_pct": round(cvar_95 * 100, 3),
            "correlation_to_benchmark": round(correlation, 3) if correlation is not None else None,
            "computed_at": datetime.now().isoformat(),
        }
        
        cache.set("risk_metrics", cache_key, data=result, ttl=CacheConfig.FUNDAMENTALS)
        return result
        
    except Exception as e:
        logger.warning(f"Failed to compute risk metrics for {ticker}: {e}")
        return _empty_risk_metrics(ticker, str(e))


def _empty_risk_metrics(ticker: str, error: str = "") -> Dict[str, Any]:
    """Return an empty risk metrics dict for graceful degradation."""
    return {
        "ticker": ticker.upper(),
        "benchmark": "SPY",
        "period_days": 0,
        "beta": None,
        "annualized_return_pct": None,
        "sharpe_ratio": None,
        "sortino_ratio": None,
        "max_drawdown_pct": None,
        "volatility_annual_pct": None,
        "var_95_pct": None,
        "cvar_95_pct": None,
        "correlation_to_benchmark": None,
        "computed_at": datetime.now().isoformat(),
        "error": error,
    }
