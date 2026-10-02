"""Mode-qualified report file naming and legacy fallback resolution."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

VALID_MODES = frozenset({"quick", "standard", "deep"})


def normalize_mode(mode: Optional[str]) -> str:
    normalized = str(mode or "standard").lower().strip()
    return normalized if normalized in VALID_MODES else "standard"


def report_basename(ticker: str, date: str, mode: Optional[str] = None) -> str:
    return f"{ticker.upper()}_{date}_{normalize_mode(mode)}_report"


def legacy_report_basename(ticker: str, date: str) -> str:
    return f"{ticker.upper()}_{date}_report"


def _candidate_paths(
    output_dir: str,
    basename: str,
    suffix: str,
) -> List[Path]:
    root = Path(output_dir)
    return [
        root / "reports" / f"{basename}.{suffix}",
        root / f"{basename}.{suffix}",
    ]


def write_report_html_path(
    output_dir: str,
    ticker: str,
    date: str,
    mode: Optional[str] = None,
) -> Path:
    basename = report_basename(ticker, date, mode)
    path = Path(output_dir) / "reports" / f"{basename}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def write_report_pdf_path(
    output_dir: str,
    ticker: str,
    date: str,
    mode: Optional[str] = None,
) -> Path:
    basename = report_basename(ticker, date, mode)
    path = Path(output_dir) / "reports" / f"{basename}.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def resolve_report_html(
    output_dir: str,
    ticker: str,
    date: str,
    mode: Optional[str] = None,
) -> Optional[Path]:
    candidates = _candidate_paths(output_dir, report_basename(ticker, date, mode), "html")
    candidates.extend(
        _candidate_paths(output_dir, legacy_report_basename(ticker, date), "html")
    )
    for path in candidates:
        if path.exists():
            return path
    return None


def resolve_report_pdf(
    output_dir: str,
    ticker: str,
    date: str,
    mode: Optional[str] = None,
) -> Optional[Path]:
    candidates = _candidate_paths(output_dir, report_basename(ticker, date, mode), "pdf")
    candidates.extend(
        _candidate_paths(output_dir, legacy_report_basename(ticker, date), "pdf")
    )
    for path in candidates:
        if path.exists():
            return path
    return None
