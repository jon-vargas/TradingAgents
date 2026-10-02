import difflib
import re
from pathlib import Path
from typing import Optional


def _strip_html(text: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", text)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def diff_report_files(file_a: Path, file_b: Path) -> str:
    text_a = _strip_html(file_a.read_text(encoding="utf-8", errors="replace"))
    text_b = _strip_html(file_b.read_text(encoding="utf-8", errors="replace"))
    diff = difflib.unified_diff(
        text_a.splitlines(),
        text_b.splitlines(),
        fromfile=str(file_a),
        tofile=str(file_b),
        lineterm="",
    )
    return "\n".join(diff)


def resolve_report_path(
    output_dir: str,
    ticker: str,
    date: str,
    mode: Optional[str] = None,
) -> Path:
    from tradingagents.reporting.report_paths import resolve_report_html

    resolved = resolve_report_html(output_dir, ticker, date, mode)
    if resolved is not None:
        return resolved
    from tradingagents.reporting.report_paths import legacy_report_basename

    return Path(output_dir) / "reports" / f"{legacy_report_basename(ticker, date)}.html"


def diff_reports(
    *,
    file_a: Optional[str] = None,
    file_b: Optional[str] = None,
    ticker: Optional[str] = None,
    date_a: Optional[str] = None,
    date_b: Optional[str] = None,
    output_dir: str = "research_output",
) -> str:
    if file_a and file_b:
        path_a = Path(file_a)
        path_b = Path(file_b)
    elif ticker and date_a and date_b:
        path_a = resolve_report_path(output_dir, ticker, date_a)
        path_b = resolve_report_path(output_dir, ticker, date_b)
    else:
        raise ValueError("Provide file_a/file_b or ticker with date_a/date_b.")

    if not path_a.exists() or not path_b.exists():
        raise FileNotFoundError(f"Missing file(s): {path_a} / {path_b}")
    return diff_report_files(path_a, path_b)
