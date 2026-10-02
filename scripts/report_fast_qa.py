#!/usr/bin/env python3
import argparse
import json
import re
import sys
from difflib import unified_diff
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.reporting import generate_html_report
from tradingagents.reporting.pdf_generator import apply_report_qc, build_report_payload
from tradingagents.reporting.attribution import lint_signal_json_sections


FIXTURES_DIR = ROOT_DIR / "tests" / "fixtures"
GOLDENS_DIR = ROOT_DIR / "tests" / "goldens"


def load_fixture(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
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


def extract_plan_values(html: str) -> dict:
    def find_value(label: str) -> str:
        pattern = rf'<div class="plan-label">{label}</div>\s*<div class="plan-value [^"]+">([^<]+)</div>'
        match = re.search(pattern, html)
        return match.group(1).strip() if match else ""

    return {
        "entry": find_value("Entry Price"),
        "stop": find_value("Stop Loss"),
        "target_1": find_value("Target 1"),
        "target_2": find_value("Target 2"),
    }


def extract_risk_texts(html: str) -> list:
    return [text.strip() for text in re.findall(r'class="risk-content">([^<]+)</div>', html)]


def qa_checks(html: str) -> list:
    issues = []

    summary_text = extract_summary_text(html)
    if not summary_text:
        issues.append("missing_executive_summary")
    if re.search(r"\*\*|__|#+\s", summary_text):
        issues.append("summary_contains_raw_markdown")

    plan = extract_plan_values(html)
    for key in ("entry", "stop", "target_1"):
        value = plan.get(key, "")
        if not re.match(r"^\$?\d+\.\d{2}$", value):
            issues.append(f"invalid_{key}")

    target_2 = plan.get("target_2", "")
    if target_2 not in {"$N/A", "N/A"} and not re.match(r"^\$?\d+\.\d{2}$", target_2):
        issues.append("invalid_target_2")

    risks = extract_risk_texts(html)
    if not risks:
        issues.append("missing_risk_cards")
    for text in risks:
        if "..." in text:
            issues.append("risk_truncation")
            break
        if not re.search(r"[.!?]$", text):
            issues.append("risk_incomplete_sentence")
            break

    return issues


def _normalize_html_for_golden(html: str) -> str:
    normalized = re.sub(
        r'(<span class="meta-label">Generated:</span>\s*<span class="meta-value">)([^<]+)(</span>)',
        r"\1<generated-at>\3",
        html,
    )
    return normalized


def compare_golden(golden_path: Path, html: str) -> str:
    if not golden_path.exists():
        return f"missing_golden:{golden_path.name}"

    expected = golden_path.read_text(encoding="utf-8")
    expected_norm = _normalize_html_for_golden(expected)
    actual_norm = _normalize_html_for_golden(html)

    if expected_norm == actual_norm:
        return ""

    diff = unified_diff(
        expected_norm.splitlines(),
        actual_norm.splitlines(),
        fromfile=str(golden_path),
        tofile="generated",
        lineterm="",
    )
    return "\n".join(diff)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fast QA for report rendering.")
    parser.add_argument("--fixtures", nargs="*", help="Fixture JSON paths.")
    parser.add_argument("--update-goldens", action="store_true", help="Update golden HTML files.")
    args = parser.parse_args()

    fixture_paths = [Path(p) for p in (args.fixtures or [])]
    if not fixture_paths:
        fixture_paths = sorted(FIXTURES_DIR.glob("*_state.json"))

    if not fixture_paths:
        print("No fixtures found.")
        return 1

    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)

    any_failures = False
    for fixture_path in fixture_paths:
        state = load_fixture(fixture_path)
        ticker = state.get("company_of_interest") or fixture_path.stem.split("_")[0]
        analysis_date = state.get("trade_date", "2026-01-01")

        html = render_html(state, ticker, analysis_date)
        issues = qa_checks(html)
        payload = build_report_payload(
            state=state,
            ticker=ticker,
            analysis_date=analysis_date,
            decision=state.get("final_trade_decision", "HOLD"),
            config=DEFAULT_CONFIG.copy(),
            duration_seconds=225.0,
        )
        issues.extend(apply_report_qc(payload, state=state))
        issues.extend(lint_signal_json_sections(state))

        golden_path = GOLDENS_DIR / f"{ticker}.html"
        diff_text = ""
        if args.update_goldens:
            golden_path.write_text(html, encoding="utf-8")
            print(f"[golden updated] {ticker} -> {golden_path}")
        else:
            diff_text = compare_golden(golden_path, html)
            if diff_text:
                if diff_text.startswith("missing_golden:"):
                    issues.append(diff_text)
                else:
                    issues.append("golden_mismatch")

        if issues:
            any_failures = True
            print(f"[fail] {ticker}: {', '.join(i for i in issues)}")
            if diff_text and diff_text != "" and not diff_text.startswith("missing_golden:"):
                print(diff_text)
            continue

        print(f"[pass] {ticker}")

    return 1 if any_failures else 0


if __name__ == "__main__":
    sys.exit(main())
