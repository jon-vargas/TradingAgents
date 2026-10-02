#!/usr/bin/env python3
"""Register HTML reports on disk that have no matching ``analyses`` row.

Finds ``research_output/reports/{TICKER}_{DATE}_report.html`` files whose
(ticker, analysis_date) pair is absent from the DB, parses summary fields
from the saved HTML, and inserts a minimal ``Analysis`` row so History &
Reports and ``/history/{id}`` work (report body still loads from disk).

Usage
-----
    python scripts/backfill_orphan_analyses.py [--dry-run]
    python scripts/backfill_orphan_analyses.py --apply
    python scripts/backfill_orphan_analyses.py --apply --ticker LUV --date 2026-09-08
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tradingagents.reporting.database import Analysis, ResearchDatabase, get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_orphan_analyses")

REPORT_NAME_RE = re.compile(
    r"^([A-Z0-9.-]+)_(\d{4}-\d{2}-\d{2})(?:_(quick|standard|deep))?_report\.html$"
)
DECISION_JSON_RE = re.compile(
    r"DECISION_JSON:\s*(\{.*?\})(?:</p>|$)",
    re.DOTALL,
)
DECISION_CHIP_RE = re.compile(
    r'<div class="decision-chip\s+(buy|sell|hold)[^"]*">\s*([^<]+)\s*</div>',
    re.IGNORECASE,
)
METRIC_RE = re.compile(
    r'<div class="metric-label">([^<]+)</div>\s*</div>\s*</div>',
    re.IGNORECASE,
)
METRIC_VALUE_RE = re.compile(
    r'<div class="metric-value">([^<]+)</div>\s*<div class="metric-label">([^<]+)</div>',
    re.IGNORECASE,
)
GENERATED_RE = re.compile(
    r'<span class="meta-label">Generated:</span>\s*<span class="meta-value">([^<]+)</span>',
)
MODEL_RE = re.compile(
    r'<span class="meta-label">Model:</span>\s*<span class="meta-value">([^<]+)</span>',
)
PROFILE_LIMITS_RE = re.compile(
    r"Profile limits \(([^)]+)\)",
    re.IGNORECASE,
)
INVESTMENT_PROFILE_RE = re.compile(
    r"<strong>Investment profile:</strong>\s*([^<]+)",
    re.IGNORECASE,
)
DURATION_RE = re.compile(r"(\d+)\s*m\s*(\d+)\s*s", re.IGNORECASE)


def parse_report_filename(path: Path) -> Optional[Tuple[str, str]]:
    match = REPORT_NAME_RE.match(path.name)
    if not match:
        return None
    return match.group(1).upper(), match.group(2)


def _parse_duration_seconds(text: str) -> float:
    match = DURATION_RE.search(text or "")
    if not match:
        return 0.0
    return float(int(match.group(1)) * 60 + int(match.group(2)))


def _parse_metrics(html: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for value, label in METRIC_VALUE_RE.findall(html):
        out[label.strip().lower()] = value.strip()
    return out


def _normalize_risk_profile(raw: str) -> str:
    key = (raw or "").strip().lower()
    if key in {"aggressive", "growth", "conservative"}:
        return key
    if "aggressive" in key:
        return "aggressive"
    if "conservative" in key:
        return "conservative"
    return "growth"


def _infer_analysis_mode(html: str) -> str:
    lower = html.lower()
    if "<h4>deep research</h4>" in lower and (
        "get_deep_research" in lower
        or "get_sec_filings_snapshot" in lower
        or "get_earnings_transcript_snapshot" in lower
    ):
        return "deep"
    if "estimated time" in lower and "2-3" in lower:
        return "quick"
    return "standard"


def _parse_decision(html: str) -> Tuple[str, str, str]:
    """Return (decision, position_action, decision_json_raw)."""
    json_match = DECISION_JSON_RE.search(html)
    if json_match:
        raw = json_match.group(1).strip()
        try:
            payload = json.loads(raw)
            decision = str(payload.get("decision") or "").upper()
            position = str(payload.get("position_action") or "").upper()
            if decision:
                return decision, position, raw
        except json.JSONDecodeError:
            pass

    chip = DECISION_CHIP_RE.search(html)
    if chip:
        decision = chip.group(1).upper()
        label = chip.group(2).strip()
        position = ""
        if "(" in label and ")" in label:
            position = label.split("(", 1)[1].split(")", 1)[0].strip().upper()
        return decision, position, ""

    return "HOLD", "", ""


def parse_orphan_report_html(html: str) -> Dict[str, Any]:
    metrics = _parse_metrics(html)
    decision, position_action, decision_json = _parse_decision(html)
    confidence_raw = metrics.get("confidence", "0").replace("%", "")
    quality_raw = metrics.get("data quality", "0").replace("%", "")
    debate_raw = metrics.get("debate rounds", "0")

    generated = (GENERATED_RE.search(html) or [None, ""])[1].strip()
    created_at = datetime.now().isoformat()
    if generated:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                created_at = datetime.strptime(generated, fmt).isoformat()
                break
            except ValueError:
                continue

    model_meta = (MODEL_RE.search(html) or [None, ""])[1].strip()
    llm_provider, deep_model = "", ""
    if " / " in model_meta:
        llm_provider, deep_model = [part.strip() for part in model_meta.split(" / ", 1)]
    elif model_meta:
        deep_model = model_meta

    risk_raw = (PROFILE_LIMITS_RE.search(html) or [None, "Growth"])[1]
    inv_raw = (INVESTMENT_PROFILE_RE.search(html) or [None, ""])[1].strip()

    investment_profile = ""
    if inv_raw:
        display = inv_raw.split("(", 1)[0].strip()
        investment_profile = json.dumps(
            {
                "display_name": display,
                "profile_key": display.lower().replace(" ", "_"),
                "resolved_from": "orphan_html_backfill",
            }
        )

    try:
        confidence = float(confidence_raw)
    except ValueError:
        confidence = 0.0
    try:
        data_quality_score = int(float(quality_raw))
    except ValueError:
        data_quality_score = 0
    try:
        debate_rounds = int(float(debate_raw))
    except ValueError:
        debate_rounds = 0

    return {
        "decision": decision,
        "position_action": position_action,
        "decision_json": decision_json,
        "confidence": confidence,
        "data_quality_score": data_quality_score,
        "debate_rounds": debate_rounds,
        "duration_seconds": _parse_duration_seconds(metrics.get("analysis time", "")),
        "created_at": created_at,
        "llm_provider": llm_provider,
        "deep_think_model": deep_model,
        "risk_profile": _normalize_risk_profile(risk_raw),
        "analysis_mode": _infer_analysis_mode(html),
        "investment_profile": investment_profile,
        "final_decision": json_match.group(0) if (json_match := DECISION_JSON_RE.search(html)) else "",
        "notes": "Backfilled from orphan HTML report (DB save failed during original run).",
        "tags": "orphan-backfill",
    }


def find_orphan_reports(
    db: ResearchDatabase,
    reports_dir: Path,
    ticker: Optional[str] = None,
    date: Optional[str] = None,
) -> List[Tuple[Path, str, str]]:
    with db._connect() as conn:
        rows = conn.execute("SELECT ticker, analysis_date FROM analyses").fetchall()
    db_keys = {(str(r[0]).upper(), str(r[1])) for r in rows}

    orphans: List[Tuple[Path, str, str]] = []
    for path in sorted(reports_dir.glob("*_report.html")):
        parsed = parse_report_filename(path)
        if not parsed:
            continue
        sym, adate = parsed
        if ticker and sym != ticker.upper():
            continue
        if date and adate != date:
            continue
        if (sym, adate) not in db_keys:
            orphans.append((path, sym, adate))
    return orphans


def build_analysis_record(ticker: str, analysis_date: str, parsed: Dict[str, Any]) -> Analysis:
    return Analysis(
        ticker=ticker,
        analysis_date=analysis_date,
        created_at=parsed["created_at"],
        decision=parsed["decision"],
        position_action=parsed["position_action"],
        confidence=parsed["confidence"],
        llm_provider=parsed["llm_provider"],
        deep_think_model=parsed["deep_think_model"],
        debate_rounds=parsed["debate_rounds"],
        analysis_mode=parsed["analysis_mode"],
        risk_profile=parsed["risk_profile"],
        data_quality_score=parsed["data_quality_score"],
        duration_seconds=parsed["duration_seconds"],
        investment_profile=parsed["investment_profile"],
        decision_json=parsed["decision_json"],
        final_decision=parsed["final_decision"],
        notes=parsed["notes"],
        tags=parsed["tags"],
        report_warnings=json.dumps(
            [{"group": "integrity", "message": "Row backfilled from orphan HTML; agent state not recovered."}]
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill orphan analysis HTML reports into research.db")
    parser.add_argument("--db", default="research.db", help="SQLite database path")
    parser.add_argument(
        "--reports-dir",
        default="research_output/reports",
        help="Directory containing *_report.html files",
    )
    parser.add_argument("--ticker", help="Only backfill this ticker")
    parser.add_argument("--date", help="Only backfill this analysis date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    parser.add_argument("--apply", action="store_true", help="Insert missing rows")
    args = parser.parse_args()

    if not args.apply:
        args.dry_run = True

    reports_dir = (_PROJECT_ROOT / args.reports_dir).resolve()
    if not reports_dir.is_dir():
        logger.error("Reports directory not found: %s", reports_dir)
        return 1

    db = get_db(args.db)
    orphans = find_orphan_reports(db, reports_dir, ticker=args.ticker, date=args.date)
    if not orphans:
        logger.info("No orphan reports found.")
        return 0

    logger.info("Found %d orphan report(s)", len(orphans))
    inserted = 0
    for path, ticker, analysis_date in orphans:
        html = path.read_text(encoding="utf-8")
        parsed = parse_orphan_report_html(html)
        logger.info(
            "%s %s -> decision=%s mode=%s risk=%s confidence=%.0f created=%s",
            ticker,
            analysis_date,
            parsed["decision"],
            parsed["analysis_mode"],
            parsed["risk_profile"],
            parsed["confidence"],
            parsed["created_at"],
        )
        if args.dry_run:
            continue
        analysis = build_analysis_record(ticker, analysis_date, parsed)
        analysis_id = db.save_analysis(analysis)
        logger.info("  inserted analysis id=%s", analysis_id)
        inserted += 1

    if args.dry_run:
        logger.info("Dry run complete — re-run with --apply to insert rows.")
    else:
        logger.info("Backfill complete: %d row(s) inserted.", inserted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
