"""
Cleanup Manager — age-based file cleanup, storage reporting, and DB maintenance.

All destructive methods default to dry_run=True for safety.
"""

import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tradingagents.cleanup")


class CleanupManager:
    """Centralized file and database maintenance utilities."""

    def __init__(
        self,
        output_dir: str = "research_output",
        cache_dir: str = "tradingagents/dataflows/data_cache",
        eval_dir: str = "eval_results",
        log_dir: str = "logs",
        db_path: str = "research.db",
    ):
        self.output_dir = Path(output_dir)
        self.cache_dir = Path(cache_dir)
        self.eval_dir = Path(eval_dir)
        self.log_dir = Path(log_dir)
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Age-based cleanup
    # ------------------------------------------------------------------

    def cleanup_old_reports(
        self, max_age_days: int = 90, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Delete report HTML/PDF files older than *max_age_days*.

        Searches both ``research_output/reports/`` and the flat root for
        backward compatibility with pre-reorganization files.
        """
        cutoff = time.time() - max_age_days * 86400
        removed: List[str] = []
        errors: List[str] = []

        search_dirs = [self.output_dir / "reports", self.output_dir]
        for search_dir in search_dirs:
            if not search_dir.is_dir():
                continue
            for pattern in ("*_report.html", "*_report.pdf"):
                for path in search_dir.glob(pattern):
                    if path.stat().st_mtime < cutoff:
                        logger.info(
                            "%s old report: %s",
                            "Would remove" if dry_run else "Removing",
                            path,
                        )
                        if not dry_run:
                            try:
                                path.unlink()
                                removed.append(str(path))
                            except OSError as exc:
                                errors.append(f"{path}: {exc}")
                        else:
                            removed.append(str(path))

        return {"removed": removed, "count": len(removed), "dry_run": dry_run, "errors": errors}

    def cleanup_old_eval_results(
        self, max_age_days: int = 30, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Delete eval_results state logs older than *max_age_days*."""
        cutoff = time.time() - max_age_days * 86400
        removed: List[str] = []
        errors: List[str] = []

        if not self.eval_dir.is_dir():
            return {"removed": [], "count": 0, "dry_run": dry_run, "errors": []}

        for path in self.eval_dir.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                logger.info(
                    "%s old eval log: %s",
                    "Would remove" if dry_run else "Removing",
                    path,
                )
                if not dry_run:
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except OSError as exc:
                        errors.append(f"{path}: {exc}")
                else:
                    removed.append(str(path))

        return {"removed": removed, "count": len(removed), "dry_run": dry_run, "errors": errors}

    def cleanup_stale_cache(self) -> int:
        """Wrapper around DataCache.cleanup_expired() for use outside the web app."""
        try:
            from tradingagents.dataflows.cache import get_cache
            cache = get_cache()
            return cache.cleanup_expired()
        except Exception as exc:
            logger.warning("Cache cleanup failed: %s", exc)
            return 0

    def cleanup_old_cache_csvs(
        self, max_age_days: int = 7, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Remove legacy CSV files from data_cache/ older than *max_age_days*."""
        cutoff = time.time() - max_age_days * 86400
        removed: List[str] = []
        errors: List[str] = []

        if not self.cache_dir.is_dir():
            return {"removed": [], "count": 0, "dry_run": dry_run, "errors": []}

        for path in self.cache_dir.glob("*.csv"):
            if path.stat().st_mtime < cutoff:
                logger.info(
                    "%s old cache CSV: %s",
                    "Would remove" if dry_run else "Removing",
                    path,
                )
                if not dry_run:
                    try:
                        path.unlink()
                        removed.append(str(path))
                    except OSError as exc:
                        errors.append(f"{path}: {exc}")
                else:
                    removed.append(str(path))

        return {"removed": removed, "count": len(removed), "dry_run": dry_run, "errors": errors}

    # ------------------------------------------------------------------
    # Database maintenance
    # ------------------------------------------------------------------

    def vacuum_database(self) -> Dict[str, Any]:
        """Run VACUUM and ANALYZE on the SQLite database to reclaim space."""
        if not self.db_path.is_file():
            return {"success": False, "error": "Database file not found"}

        size_before = self.db_path.stat().st_size
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.close()
        except Exception as exc:
            return {"success": False, "error": str(exc)}

        size_after = self.db_path.stat().st_size
        saved = size_before - size_after
        logger.info(
            "Database vacuumed: %s -> %s (saved %s)",
            _fmt_bytes(size_before),
            _fmt_bytes(size_after),
            _fmt_bytes(saved),
        )
        return {
            "success": True,
            "size_before": size_before,
            "size_after": size_after,
            "bytes_saved": saved,
        }

    def cleanup_db_backups(
        self, max_keep: int = 5, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Keep only the *max_keep* most recent database backup files.

        Backups are stored in a ``backups/`` subdirectory adjacent to the
        database file to keep the project root clean.
        """
        backup_dir = self.db_path.parent / "backups"
        if not backup_dir.is_dir():
            return {
                "total_backups": 0,
                "kept": 0,
                "removed": [],
                "count": 0,
                "dry_run": dry_run,
                "errors": [],
            }
        backups = sorted(
            backup_dir.glob(f"{self.db_path.name}.backup_*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        to_remove = backups[max_keep:]
        removed: List[str] = []
        errors: List[str] = []

        for path in to_remove:
            logger.info(
                "%s old backup: %s",
                "Would remove" if dry_run else "Removing",
                path,
            )
            if not dry_run:
                try:
                    path.unlink()
                    removed.append(str(path))
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
            else:
                removed.append(str(path))

        return {
            "total_backups": len(backups),
            "kept": min(len(backups), max_keep),
            "removed": removed,
            "count": len(removed),
            "dry_run": dry_run,
            "errors": errors,
        }

    def cleanup_movers_data(
        self, max_age_days: int = 90, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Purge old movers snapshots and movers strategy side-table rows."""
        try:
            from tradingagents.reporting import ResearchDatabase

            db = ResearchDatabase(str(self.db_path))
            return db.purge_old_movers_data(max_age_days=max_age_days, dry_run=dry_run)
        except Exception as exc:
            logger.warning("Movers cleanup failed: %s", exc)
            return {"dry_run": dry_run, "error": str(exc), "movers_snapshots": 0, "movers_run_details": 0}

    # ------------------------------------------------------------------
    # Combined cleanup
    # ------------------------------------------------------------------

    def cleanup_all(
        self, max_age_days: int = 90, dry_run: bool = True
    ) -> Dict[str, Any]:
        """Run all age-based cleanup tasks at once."""
        return {
            "reports": self.cleanup_old_reports(max_age_days=max_age_days, dry_run=dry_run),
            "eval_results": self.cleanup_old_eval_results(max_age_days=min(max_age_days, 30), dry_run=dry_run),
            "cache_csvs": self.cleanup_old_cache_csvs(max_age_days=7, dry_run=dry_run),
		"db_backups": self.cleanup_db_backups(max_keep=3, dry_run=dry_run),
            "movers": self.cleanup_movers_data(max_age_days=max_age_days, dry_run=dry_run),
        }

    # ------------------------------------------------------------------
    # Storage reporting
    # ------------------------------------------------------------------

    def get_storage_report(self) -> Dict[str, Any]:
        """Return per-directory file counts and sizes for display in CLI and web UI."""
        report: Dict[str, Any] = {}

        for label, directory in [
            ("research_output", self.output_dir),
            ("data_cache", self.cache_dir),
            ("eval_results", self.eval_dir),
            ("logs", self.log_dir),
        ]:
            report[label] = _dir_stats(directory)

        if self.db_path.is_file():
            report["database"] = {
                "file_count": 1,
                "size_bytes": self.db_path.stat().st_size,
                "size_mb": round(self.db_path.stat().st_size / (1024 * 1024), 2),
            }
            try:
                conn = sqlite3.connect(str(self.db_path))
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM movers_snapshots")
                movers_snapshots = int(cur.fetchone()[0])
                cur.execute("SELECT COUNT(*) FROM movers_run_details")
                movers_run_details = int(cur.fetchone()[0])
                conn.close()
                report["database"]["movers_snapshots_rows"] = movers_snapshots
                report["database"]["movers_run_details_rows"] = movers_run_details
            except Exception:
                report["database"]["movers_snapshots_rows"] = 0
                report["database"]["movers_run_details_rows"] = 0
        else:
            report["database"] = {"file_count": 0, "size_bytes": 0, "size_mb": 0.0}

        # Database backups stored in backups/ subdirectory
        db_dir = self.db_path.parent
        backup_dir = db_dir / "backups"
        backup_files = sorted(backup_dir.glob(f"{self.db_path.name}.backup_*")) if backup_dir.is_dir() else []
        backup_bytes = sum(p.stat().st_size for p in backup_files if p.is_file())
        report["db_backups"] = {
            "file_count": len(backup_files),
            "size_bytes": backup_bytes,
            "size_mb": round(backup_bytes / (1024 * 1024), 2),
        }

        total_bytes = sum(v.get("size_bytes", 0) for v in report.values())
        report["total"] = {
            "size_bytes": total_bytes,
            "size_mb": round(total_bytes / (1024 * 1024), 2),
        }
        return report


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _dir_stats(directory: Path) -> Dict[str, Any]:
    """Compute file count and total size for a directory tree."""
    if not directory.is_dir():
        return {"file_count": 0, "size_bytes": 0, "size_mb": 0.0}
    total_size = 0
    file_count = 0
    for path in directory.rglob("*"):
        if path.is_file():
            file_count += 1
            total_size += path.stat().st_size
    return {
        "file_count": file_count,
        "size_bytes": total_size,
        "size_mb": round(total_size / (1024 * 1024), 2),
    }


def _fmt_bytes(n: int) -> str:
    """Format byte count for human display."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
