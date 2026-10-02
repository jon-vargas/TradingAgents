#!/usr/bin/env python3
import argparse
import csv
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from tradingagents.backtesting import compute_calibration


def write_csv(output_path: Path, bins):
    with output_path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["avg_confidence", "accuracy", "count"])
        for avg_conf, accuracy, count in bins:
            writer.writerow([avg_conf, accuracy, count])


def write_html(output_path: Path, bins, *, bins_count: int, min_samples: int, total_samples: int, sample_limit):
    rows = []
    chart_rows = []
    bin_size = 100.0 / bins_count if bins_count else 100.0
    for idx, (avg_conf, accuracy, count) in enumerate(bins):
        label_start = int(round(idx * bin_size))
        label_end = int(round((idx + 1) * bin_size))
        label = f"{label_start}-{label_end}"
        if avg_conf < 0 or accuracy < 0:
            avg_conf_display = "n/a"
            accuracy_display = "n/a"
            chart_rows.append(
                f"""
                <div class="chart-row">
                  <div class="chart-label">{label}</div>
                  <div class="chart-track chart-disabled"><span class="chart-na">n/a</span></div>
                  <div class="chart-count">{count}</div>
                </div>
                """
            )
        else:
            avg_conf_display = f"{avg_conf:.2f}%"
            accuracy_display = f"{accuracy:.1%}"
            accuracy_pct = min(100.0, max(0.0, accuracy * 100.0))
            conf_pct = min(100.0, max(0.0, avg_conf))
            chart_rows.append(
                f"""
                <div class="chart-row">
                  <div class="chart-label">{label}</div>
                  <div class="chart-track">
                    <div class="chart-bar" style="width: {accuracy_pct:.1f}%;"></div>
                    <div class="chart-marker" style="left: {conf_pct:.1f}%;"></div>
                  </div>
                  <div class="chart-count">{count}</div>
                </div>
                """
            )
        rows.append(
            f"<tr><td>{avg_conf_display}</td><td>{accuracy_display}</td><td>{count}</td></tr>"
        )

    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Confidence Calibration Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; padding: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
    th {{ background: #f5f5f5; }}
    .meta {{ color: #555; font-size: 14px; }}
    .legend {{ display: flex; gap: 16px; font-size: 12px; margin: 8px 0 12px; }}
    .legend-item {{ display: flex; align-items: center; gap: 6px; }}
    .legend-swatch {{ width: 10px; height: 10px; border-radius: 2px; display: inline-block; }}
    .legend-accuracy {{ background: #4c7ef3; }}
    .legend-confidence {{ background: #e76f51; }}
    .chart {{ margin: 16px 0 24px; }}
    .chart-row {{ display: flex; align-items: center; gap: 12px; margin: 8px 0; }}
    .chart-label {{ width: 72px; font-size: 12px; color: #555; }}
    .chart-track {{ position: relative; flex: 1; height: 14px; background: #f0f0f0; border-radius: 7px; }}
    .chart-disabled {{ display: flex; align-items: center; justify-content: center; font-size: 11px; color: #999; }}
    .chart-bar {{ position: absolute; left: 0; top: 0; height: 100%; background: #4c7ef3; border-radius: 7px; }}
    .chart-marker {{ position: absolute; top: -3px; width: 2px; height: 20px; background: #e76f51; }}
    .chart-count {{ width: 48px; text-align: right; font-size: 12px; color: #555; }}
  </style>
</head>
<body>
  <h1>Confidence Calibration</h1>
  <p class="meta">
    Total backtested samples: {total_samples} |
    Min samples/bin: {min_samples} |
    Sample limit: {sample_limit if sample_limit else "all"}
  </p>
  <div class="legend">
    <div class="legend-item"><span class="legend-swatch legend-accuracy"></span>Accuracy</div>
    <div class="legend-item"><span class="legend-swatch legend-confidence"></span>Avg Confidence</div>
  </div>
  <div class="chart">
    {"".join(chart_rows)}
  </div>
  <table>
    <thead>
      <tr>
        <th>Avg Confidence</th>
        <th>Accuracy</th>
        <th>Count</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate confidence calibration report.")
    parser.add_argument("--db-path", help="Path to research.db", default=None)
    parser.add_argument("--bins", type=int, default=None)
    parser.add_argument("--min-samples", type=int, default=None)
    parser.add_argument("--sample-limit", type=int, default=None)
    parser.add_argument("--output-dir", default="research_output", help="Output directory")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    calibration_dir = output_dir / "calibration"
    calibration_dir.mkdir(parents=True, exist_ok=True)

    calibration = compute_calibration(
        bins=args.bins,
        min_samples=args.min_samples,
        sample_limit=args.sample_limit,
        db_path=args.db_path,
    )
    bins = calibration.get("bins", [])
    meta = calibration.get("meta", {})
    bins_count = meta.get("bins", len(bins))
    min_samples = meta.get("min_samples", args.min_samples or 0)
    total_samples = meta.get("total_samples", 0)
    sample_limit = meta.get("sample_limit", args.sample_limit)

    csv_path = calibration_dir / "confidence_calibration.csv"
    html_path = calibration_dir / "confidence_calibration.html"
    write_csv(csv_path, bins)
    write_html(
        html_path,
        bins,
        bins_count=bins_count,
        min_samples=min_samples,
        total_samples=total_samples,
        sample_limit=sample_limit,
    )

    print(f"Calibration CSV saved to: {csv_path}")
    print(f"Calibration HTML saved to: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
