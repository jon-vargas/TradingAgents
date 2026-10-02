import json
import re
import unittest
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting import generate_html_report


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    fixture_path = FIXTURES_DIR / name
    with fixture_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def render_html(state: dict, ticker: str, analysis_date: str) -> str:
    config = DEFAULT_CONFIG.copy()
    return generate_html_report(
        state=state,
        ticker=ticker,
        analysis_date=analysis_date,
        decision=state.get("final_trade_decision", "HOLD"),
        config=config,
        duration_seconds=225.0,
        output_path=None,
    )


def extract_summary_text(html: str) -> str:
    match = re.search(r'class="summary-text">([^<]+)</div>', html)
    return match.group(1).strip() if match else ""


def test_technical_report_limit_preserves_live_sized_market_sections():
    from tradingagents.reporting.pdf_generator import smart_truncate_section

    market_report = "technical evidence " * 430  # ~8.2K characters
    rendered = smart_truncate_section(market_report, "technical")
    assert rendered == market_report


def extract_plan_values(html: str) -> dict:
    def find_value(label: str) -> str:
        pattern = rf'<div class="plan-label">{label}</div>\s*<div class="plan-value [^"]+">([^<]+)</div>'
        match = re.search(pattern, html)
        return match.group(1).strip() if match else ""

    return {
        "entry": find_value("Entry Price") or find_value("Mark"),
        "stop": find_value("Stop Loss") or find_value("Invalidation"),
        "target_1": find_value("Target 1") or find_value("Repair"),
        "target_2": find_value("Target 2"),
    }


def extract_risk_texts(html: str) -> list:
    return [text.strip() for text in re.findall(r'class="risk-content">([^<]+)</div>', html)]


class ReportRenderingTests(unittest.TestCase):
    def assert_common_quality(self, html: str):
        self.assertIn("Executive Summary", html)
        self.assertIn("Section Attribution", html)
        self.assertNotIn("SIGNAL_JSON", html)

        summary_text = extract_summary_text(html)
        self.assertTrue(summary_text)
        self.assertNotRegex(summary_text, r"\*\*|__|#+\s")

        plan_values = extract_plan_values(html)
        self.assertRegex(plan_values["entry"], r"^\$?\d+\.\d{2}$")
        self.assertRegex(plan_values["stop"], r"^\$?\d+\.\d{2}$")
        self.assertRegex(plan_values["target_1"], r"^\$?\d+\.\d{2}$")
        self.assertTrue(plan_values["target_2"] in {"$N/A", "N/A"} or re.match(r"^\$?\d+\.\d{2}$", plan_values["target_2"]))

        risk_texts = extract_risk_texts(html)
        self.assertTrue(risk_texts)
        for text in risk_texts:
            self.assertNotIn("...", text)
            self.assertRegex(text, r"[.!?]$")

        confidence_match = re.search(r'class="metric-value">(\d+)%</div>\s*<div class="metric-label">Confidence</div>', html)
        self.assertTrue(confidence_match)
        if confidence_match:
            score = int(confidence_match.group(1))
            self.assertGreaterEqual(score, 0)
            self.assertLessEqual(score, 100)

    def test_pltr_fixture(self):
        state = load_fixture("PLTR_state.json")
        html = render_html(state, "PLTR", "2026-01-09")
        self.assert_common_quality(html)

    def test_joby_fixture(self):
        state = load_fixture("JOBY_state.json")
        html = render_html(state, "JOBY", "2026-01-09")
        self.assert_common_quality(html)

    def test_asts_fixture(self):
        state = load_fixture("ASTS_state.json")
        html = render_html(state, "ASTS", "2026-01-27")
        self.assert_common_quality(html)

    def test_snapshot_sections_render(self):
        state = load_fixture("PLTR_state.json")
        state["sec_filings_snapshot"] = json.dumps(
            {
                "ticker": "PLTR",
                "company": "Palantir Technologies",
                "as_of": "2026-01-27",
                "filings": [
                    {
                        "form": "10-K",
                        "filing_date": "2026-02-15",
                        "period_end": "2025-12-31",
                        "highlights": ["Revenue growth", "Margin expansion"],
                        "risk_factors": ["Valuation risk"],
                        "material_events": [],
                    }
                ],
                "insider_activity": [
                    {
                        "name": "Executive",
                        "role": "CFO",
                        "transaction_type": "buy",
                        "date": "2026-02-20",
                    }
                ],
            }
        )
        state["earnings_transcript_snapshot"] = json.dumps(
            {
                "ticker": "PLTR",
                "company": "Palantir Technologies",
                "period": "FY2025 Q4",
                "call_date": "2026-02-14",
                "guidance": [
                    {
                        "metric": "revenue",
                        "range": "$2.6B-$2.7B",
                        "timeframe": "full year",
                        "context": "Raised outlook",
                    }
                ],
                "kpis": [
                    {
                        "name": "Commercial customers",
                        "value": "543",
                        "period": "Q4",
                        "context": "Up 20% YoY",
                    }
                ],
                "key_quotes": [
                    {"speaker": "CEO", "quote": "Demand remains strong.", "topic": "Outlook"}
                ],
            }
        )
        html = render_html(state, "PLTR", "2026-01-27")
        self.assertIn("Filings &amp; Earnings Highlights", html)
        self.assertIn("SEC Filings Snapshot", html)
        self.assertIn("Earnings Transcript Snapshot", html)

    def test_generate_report_keeps_html_when_pdf_fails(self):
        import tempfile
        from unittest.mock import patch

        from tradingagents.reporting.pdf_generator import generate_report

        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "tradingagents.reporting.pdf_generator.generate_html_report",
                return_value="<html></html>",
            ), patch(
                "tradingagents.reporting.pdf_generator.generate_pdf_report",
                side_effect=RuntimeError("weasyprint missing"),
            ):
                paths = generate_report(
                    state={},
                    ticker="AVGO",
                    analysis_date="2026-08-23",
                    decision="SELL",
                    config=DEFAULT_CONFIG.copy(),
                    output_dir=tmp,
                    format="both",
                )
        self.assertIn("html", paths)
        self.assertTrue(
            paths["html"].endswith("AVGO_2026-08-23_standard_report.html")
        )
        self.assertNotIn("pdf", paths)


if __name__ == "__main__":
    unittest.main()
