"""Unit tests for screening OHLCV cache helpers (no live Yahoo)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.dataflows.cache import DataCache
from tradingagents.dataflows import screening_ohlcv_cache as soc
from tradingagents.dataflows.yfinance_extended import _call_with_timeout


def _sample_bars(n: int = 30) -> dict:
    dates = [(datetime(2024, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]
    vals = [100.0 + i * 0.1 for i in range(n)]
    return {
        "dates": dates,
        "close": vals,
        "volume": [1_000_000.0] * n,
        "high": [v + 1 for v in vals],
        "low": [v - 1 for v in vals],
    }


class TestSpySingletonCache:
    def test_spy_download_once_across_two_get_calls(self):
        cache = DataCache(enable_disk=False)
        calls = {"n": 0}

        def _dl():
            calls["n"] += 1
            idx = pd.date_range("2024-01-01", periods=25, freq="D")
            return pd.Series([100.0] * 25, index=idx)

        s1 = soc.get_cached_spy_close(cache, "2024-06-01", "closed", 420, _dl)
        s2 = soc.get_cached_spy_close(cache, "2024-06-01", "closed", 420, _dl)
        assert calls["n"] == 1
        assert s1 is not None and s2 is not None
        assert len(s1) == 25


class TestPerTickerAssembly:
    def test_second_chunk_downloads_only_misses(self):
        cache = DataCache(enable_disk=False)
        scan_date = "2024-06-01"
        session_tag = "closed"
        lookback = 420
        shared = ["AAA", "BBB", "CCC"]
        for sym in shared:
            soc.persist_ticker_bars(cache, sym, scan_date, session_tag, lookback, _sample_bars())

        hits, misses = soc.load_ticker_bars(cache, shared + ["DDD"], scan_date, session_tag, lookback)
        assert set(hits.keys()) == set(shared)
        assert misses == ["DDD"]

        frame = soc.synthesize_ohlcv_frame(hits, shared)
        assert frame is not None
        assert not frame.empty


class TestScreeningOhlcvSession:
    def test_call_with_timeout_skips_during_session(self):
        with soc.ScreeningOhlcvSession():
            out = _call_with_timeout(lambda: {"x": 1}, default={}, op="test")
            assert out == {}
        assert not soc.is_screening_ohlcv_session_active()

    def test_session_cleared_after_exception(self):
        try:
            with soc.ScreeningOhlcvSession():
                raise ValueError("boom")
        except ValueError:
            pass
        assert not soc.is_screening_ohlcv_session_active()


def _multi_ohlcv(tickers, start, n: int = 25) -> pd.DataFrame:
    idx = pd.date_range(start, periods=n, freq="D")
    frames = {}
    for sym in tickers:
        frames[sym] = pd.DataFrame(
            {
                "Close": [10.0 + i for i in range(n)],
                "Volume": [1_000_000.0] * n,
                "High": [11.0] * n,
                "Low": [9.0] * n,
            },
            index=idx,
        )
    if len(tickers) == 1:
        return frames[tickers[0]]
    return pd.concat(frames, axis=1, keys=list(frames.keys()))


def _engine_with_cache():
    from tradingagents.screening.engine import ScreeningEngine

    engine = ScreeningEngine(db=None)
    engine.cache = DataCache(enable_disk=False)
    engine._screening_config = {"ohlcv_lookback_days": 420, "ticker_health": {"track": False}}
    return engine


class TestEngineOverlapAndPrewarm:
    def test_second_chunk_downloads_only_new_symbols(self):
        engine = _engine_with_cache()
        shared = [f"S{i:02d}" for i in range(40)]
        first = shared + [f"A{i:02d}" for i in range(20)]
        second = shared + [f"B{i:02d}" for i in range(20)]
        downloaded: list[list[str]] = []

        def _fake_ohlcv(tickers, start, end, is_single):
            downloaded.append(list(tickers))
            return _multi_ohlcv(tickers, start)

        spy = pd.Series([400.0] * 25, index=pd.date_range("2024-01-01", periods=25, freq="D"))
        with patch.object(engine, "_download_ohlcv_with_retry", side_effect=_fake_ohlcv):
            with patch.object(engine, "_download_spy_with_retry", return_value=spy):
                engine._fetch_batch_ohlcv(first, "2024-06-01")
                engine._fetch_batch_ohlcv(second, "2024-06-01")

        assert len(downloaded) == 2
        assert len(downloaded[0]) == 60
        assert set(downloaded[1]) == {f"B{i:02d}" for i in range(20)}
        assert len(downloaded[1]) <= 20

    def test_prewarmed_symbols_skip_engine_download(self):
        engine = _engine_with_cache()
        universe = ["AAA", "BBB", "CCC", "DDD"]
        prewarm_calls: list[list[str]] = []

        def _prewarm_download(tickers, start, end, is_single):
            prewarm_calls.append(list(tickers))
            return _multi_ohlcv(tickers, start)

        soc.warm_chunk_ohlcv(
            universe,
            "2024-06-01",
            engine._screening_config,
            _prewarm_download,
            cache=engine.cache,
        )
        spy = pd.Series([400.0] * 25, index=pd.date_range("2024-01-01", periods=25, freq="D"))
        with patch.object(
            engine,
            "_download_ohlcv_with_retry",
            side_effect=AssertionError("engine should not download prewarmed symbols"),
        ):
            with patch.object(engine, "_download_spy_with_retry", return_value=spy):
                # Subset of the prewarmed universe so the chunk key does not match.
                out = engine._fetch_batch_ohlcv(["CCC", "AAA"], "2024-06-01")

        assert prewarm_calls == [universe]
        assert set(out) == {"AAA", "CCC"}
        assert len(out["AAA"]["close"]) >= 20


class TestEngineSpyPerScan:
    def test_two_chunks_one_spy_download(self):
        from tradingagents.screening.engine import ScreeningEngine

        engine = ScreeningEngine(db=None)
        engine.cache = DataCache(enable_disk=False)
        engine._screening_config = {"ohlcv_lookback_days": 420}
        spy_calls = {"n": 0}

        def _fake_spy(start, end):
            spy_calls["n"] += 1
            idx = pd.date_range(start, periods=25, freq="D")
            return pd.Series([400.0] * 25, index=idx)

        def _fake_ohlcv(tickers, start, end, is_single):
            idx = pd.date_range(start, periods=25, freq="D")
            rows = []
            for _ in tickers:
                for i, ts in enumerate(idx):
                    rows.append(
                        {
                            "Date": ts,
                            "Close": 10.0 + i,
                            "Volume": 1e6,
                            "High": 11.0,
                            "Low": 9.0,
                            "Ticker": _,
                        }
                    )
            df = pd.DataFrame(rows)
            if is_single or len(tickers) == 1:
                return df.drop(columns=["Ticker"]).set_index("Date")
            out = {}
            for sym in tickers:
                sub = df[df["Ticker"] == sym].drop(columns=["Ticker"]).set_index("Date")
                out[sym] = sub
            return pd.concat(out, axis=1, keys=list(out.keys()))

        with patch.object(engine, "_download_spy_with_retry", side_effect=_fake_spy):
            with patch.object(engine, "_download_ohlcv_with_retry", side_effect=_fake_ohlcv):
                engine._fetch_batch_ohlcv(["AAPL"], "2024-06-01")
                engine._fetch_batch_ohlcv(["MSFT"], "2024-06-01")
        assert spy_calls["n"] == 1


class TestResolveAndCacheUseCached:
    def test_stale_row_used_when_refresh_stale_false(self):
        from tradingagents.screening.ticker_resolver import resolve_and_cache

        db = MagicMock()
        old = (datetime.now() - timedelta(days=20)).isoformat()
        db.get_stale_tickers.return_value = ["AAA"]
        db.get_ticker_metadata_bulk.return_value = {
            "AAA": {
                "ticker": "AAA",
                "resolved_preset": "momentum_hunter",
                "market_cap_tier": "large",
                "last_updated": old,
            }
        }
        out = resolve_and_cache(["AAA"], db, refresh_stale=False, cached_max_age_days=30)
        assert "AAA" in out
        db.get_stale_tickers.assert_not_called()
