import csv
import tempfile
from pathlib import Path

from tradingagents.research import ResearchAgent, ResearchResult


class _DummyAgent:
    def __init__(self, output_dir: str):
        self.output_dir = output_dir


def test_batch_summary_includes_snapshot_columns():
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy = _DummyAgent(temp_dir)
        results = [
            ResearchResult(
                ticker="PLTR",
                analysis_date="2026-01-27",
                decision="SELL",
                state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": ""},
                duration_seconds=12.3,
                html_path="pltr.html",
                pdf_path="pltr.pdf",
                analysis_id=1,
            )
        ]
        csv_path = Path(temp_dir) / "summary.csv"
        ResearchAgent._write_batch_summary(dummy, results, "2026-01-27", str(csv_path))

        with csv_path.open("r", newline="") as handle:
            reader = csv.reader(handle)
            header = None
            for row in reader:
                if row and row[0] == "ticker":
                    header = row
                    break
            data_row = next(reader)

        assert header is not None
        assert "sec_snapshot" in header
        assert "transcript_snapshot" in header
        assert "sec_snapshot_path" in header
        assert "transcript_snapshot_path" in header
        sec_idx = header.index("sec_snapshot")
        transcript_idx = header.index("transcript_snapshot")
        sec_path_idx = header.index("sec_snapshot_path")
        transcript_path_idx = header.index("transcript_snapshot_path")
        assert data_row[sec_idx] == "yes"
        assert data_row[transcript_idx] == "no"
        assert data_row[sec_path_idx] == "PLTR_sec_snapshot_2026-01-27.json"
        assert data_row[transcript_path_idx] == ""


def test_batch_summary_html_includes_snapshot_headers():
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy = _DummyAgent(temp_dir)
        results = [
            ResearchResult(
                ticker="PLTR",
                analysis_date="2026-01-27",
                decision="SELL",
                state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": "y"},
                duration_seconds=12.3,
                html_path="pltr.html",
                pdf_path="pltr.pdf",
                analysis_id=1,
            )
        ]
        html_path = Path(temp_dir) / "summary.html"
        ResearchAgent._write_batch_summary_html(dummy, results, "2026-01-27", str(html_path))
        html = html_path.read_text(encoding="utf-8")

        assert "SEC Snapshot" in html
        assert "Transcript Snapshot" in html
        assert "Snapshot Links" in html
        assert "<td>Yes</td><td>Yes</td>" in html
        assert "SEC snapshots" in html
        assert "Snapshot filter" in html
        assert "batch_summary_only_2026-01-27.csv" in html
        assert "batch_summary_meta_2026-01-27.csv" in html
        assert (Path(temp_dir) / "PLTR_sec_snapshot_2026-01-27.json").exists()
        assert (Path(temp_dir) / "PLTR_transcript_snapshot_2026-01-27.json").exists()
        assert (Path(temp_dir) / "batch_watchlist_brief_2026-01-27.md").exists()
        assert (Path(temp_dir) / "batch_watchlist_brief_2026-01-27.json").exists()
        assert (Path(temp_dir) / "batch_watchlist_brief_2026-01-27.csv").exists()
        assert "Avg duration" in html
        assert "P50" in html
        assert "P90" in html
        assert "Avg confidence" in html
        assert "Avg quality" in html
        assert "QA warnings" in html
        assert "Top themes" in html
        assert "Top confidence" in html
        assert "Top quality" in html
        assert "Most warnings" in html
        assert "Comparative Rankings" in html
        assert "batch_watchlist_brief_2026-01-27.md" in html
        assert "batch_watchlist_brief_2026-01-27.json" in html
        assert "batch_watchlist_brief_2026-01-27.csv" in html
        assert "batch_summary_2026-01-27_sec.html" in html
        assert "batch_summary_2026-01-27_transcript.html" in html


def test_batch_summary_only_csv():
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy = _DummyAgent(temp_dir)
        results = [
            ResearchResult(
                ticker="PLTR",
                analysis_date="2026-01-27",
                decision="SELL",
                state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": "y"},
                duration_seconds=12.3,
            )
        ]
        csv_path = Path(temp_dir) / "summary_only.csv"
        ResearchAgent._write_batch_summary_only(
            dummy,
            results,
            "2026-01-27",
            summary_path=str(csv_path),
            summary_snapshot_filter="any",
            batch_total=1,
        )

        with csv_path.open("r", newline="") as handle:
            reader = csv.reader(handle)
            rows = list(reader)

        assert rows[0] == ["summary_key", "value"]
        assert any(row[0] == "summary_total" for row in rows)
        assert any(row[0] == "sec_snapshot_paths" for row in rows)
        assert any(row[0] == "avg_duration_seconds" for row in rows)
        assert any(row[0] == "duration_p50_seconds" for row in rows)
        assert any(row[0] == "duration_p90_seconds" for row in rows)
        assert any(row[0] == "avg_confidence" for row in rows)
        assert any(row[0] == "avg_data_quality" for row in rows)
        assert any(row[0] == "qa_warning_count" for row in rows)
        assert any(row[0] == "top_themes" for row in rows)
        assert any(row[0] == "top_confidence_tickers" for row in rows)
        assert any(row[0] == "top_quality_tickers" for row in rows)
        assert any(row[0] == "top_warning_tickers" for row in rows)
        assert any(row[0] == "watchlist_brief_path" for row in rows)
        assert any(row[0] == "watchlist_brief_json_path" for row in rows)
        assert any(row[0] == "watchlist_brief_csv_path" for row in rows)


def test_batch_summary_filter_multi():
    results = [
        ResearchResult(
            ticker="AAA",
            analysis_date="2026-01-27",
            decision="BUY",
            state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": "y"},
            duration_seconds=10.0,
        ),
        ResearchResult(
            ticker="BBB",
            analysis_date="2026-01-27",
            decision="SELL",
            state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": ""},
            duration_seconds=10.0,
        ),
    ]
    filtered = ResearchAgent._filter_batch_results(results, "multi")
    assert len(filtered) == 1
    assert filtered[0].ticker == "AAA"


def test_summary_only_from_existing_csv():
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy = _DummyAgent(temp_dir)
        results = [
            ResearchResult(
                ticker="PLTR",
                analysis_date="2026-01-27",
                decision="SELL",
                state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": "y"},
                duration_seconds=12.3,
            )
        ]
        source_path = Path(temp_dir) / "summary.csv"
        ResearchAgent._write_batch_summary(dummy, results, "2026-01-27", str(source_path))
        output_path = Path(temp_dir) / "summary_only.csv"
        ResearchAgent._write_summary_only_from_csv(str(source_path), str(output_path))

        with output_path.open("r", newline="") as handle:
            rows = list(csv.reader(handle))

        assert rows[0] == ["summary_key", "value"]


def test_batch_metadata_only_csv():
    with tempfile.TemporaryDirectory() as temp_dir:
        dummy = _DummyAgent(temp_dir)
        results = [
            ResearchResult(
                ticker="PLTR",
                analysis_date="2026-01-27",
                decision="SELL",
                state={"sec_filings_snapshot": "x", "earnings_transcript_snapshot": "y"},
                duration_seconds=12.3,
            )
        ]
        csv_path = Path(temp_dir) / "summary_meta.csv"
        ResearchAgent._write_batch_metadata_only(
            dummy,
            results,
            "2026-01-27",
            summary_path=str(csv_path),
            summary_snapshot_filter="any",
            batch_total=1,
        )

        with csv_path.open("r", newline="") as handle:
            rows = list(csv.reader(handle))

        assert rows[0] == ["summary_key", "value"]
