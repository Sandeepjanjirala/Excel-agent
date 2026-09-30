"""
Media & Session Cleanup Service.

Handles automatic pruning of ephemeral datasets, workbooks, parsed pickles,
and database records older than the retention threshold (default: 12 hours).
Provides orphan file sweeping and real-time storage metrics.
"""
from __future__ import annotations

import logging
import os
import time
import threading
from datetime import timedelta
from pathlib import Path
from typing import Dict, Any, Optional

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("agent.cleanup")

# Module-level tracking for request-throttled cleanup runner
_cleanup_lock = threading.Lock()
_last_cleanup_time: float = 0.0


def _get_file_size_safe(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def cleanup_project(project_id: str | Any) -> bool:
    """
    Immediately purge a single Project, cascading to its ChatMessages,
    ExcelFiles, and unlinking all associated files on disk via signals.
    """
    from agent.models import Project

    try:
        project = Project.objects.get(id=project_id)
        project.delete()  # triggers post_delete signals for Project and ExcelFile
        logger.info("Explicitly deleted project and associated media: %s", project_id)
        return True
    except Project.DoesNotExist:
        # Check if an orphaned pickle file exists anyway
        pkl_path = Path(settings.MEDIA_ROOT) / "parsed" / f"{project_id}.pkl"
        if pkl_path.is_file():
            try:
                pkl_path.unlink()
                logger.info("Purged orphaned pickle for nonexistent project: %s", project_id)
                return True
            except OSError:
                pass
        return False
    except Exception as e:
        logger.error("Failed to delete project %s: %s", project_id, e)
        return False


def cleanup_expired_sessions(retention_hours: Optional[float] = None, dry_run: bool = False) -> Dict[str, Any]:
    """
    Purge all projects and media files older than retention_hours (default: settings.MEDIA_RETENTION_HOURS or 12).
    Also performs a sweep for orphaned media files that have no matching database record.
    """
    from agent.models import Project, ExcelFile

    if retention_hours is None:
        retention_hours = getattr(settings, "MEDIA_RETENTION_HOURS", 12)

    now = timezone.now()
    now_epoch = time.time()
    cutoff_dt = now - timedelta(hours=retention_hours)
    cutoff_epoch = now_epoch - (retention_hours * 3600)

    projects_deleted = 0
    files_deleted = 0
    bytes_freed = 0
    details = []

    media_root = Path(settings.MEDIA_ROOT)
    parsed_dir = media_root / "parsed"
    workbooks_dir = media_root / "workbooks"
    metadata_dir = media_root / "metadata"

    # Ensure directories exist
    parsed_dir.mkdir(exist_ok=True)
    workbooks_dir.mkdir(exist_ok=True)
    metadata_dir.mkdir(exist_ok=True)

    # -------------------------------------------------------------
    # 1. Inspect all Projects in the database
    # -------------------------------------------------------------
    for project in Project.objects.all():
        # Determine last activity: latest chat message or creation time
        latest_msg = project.messages.order_by("-created_at").first()
        last_active = latest_msg.created_at if latest_msg else project.created_at

        is_expired = (retention_hours <= 0) or (last_active < cutoff_dt)
        if not is_expired:
            continue

        proj_bytes = 0
        proj_files_count = 0

        # Account for pickle file
        pkl_path = parsed_dir / f"{project.id}.pkl"
        if pkl_path.is_file():
            proj_bytes += _get_file_size_safe(pkl_path)
            proj_files_count += 1

        # Account for metadata file
        if project.metadata_file and project.metadata_file.name:
            m_path = media_root / project.metadata_file.name
            if m_path.is_file():
                proj_bytes += _get_file_size_safe(m_path)
                proj_files_count += 1

        # Account for workbooks
        for ef in project.files.all():
            if ef.file and ef.file.name:
                w_path = media_root / ef.file.name
                if w_path.is_file():
                    proj_bytes += _get_file_size_safe(w_path)
                    proj_files_count += 1

        details.append({
            "project_id": str(project.id),
            "created_at": project.created_at.isoformat(),
            "last_active": last_active.isoformat(),
            "files_count": proj_files_count,
            "bytes": proj_bytes,
        })

        if not dry_run:
            # Calling delete() on the model instance fires post_delete signals,
            # which removes the files from disk and cascades to child records.
            project.delete()

        projects_deleted += 1
        files_deleted += proj_files_count
        bytes_freed += proj_bytes

    # -------------------------------------------------------------
    # 2. Orphan Sweep: Remove files on disk without DB records or older than cutoff
    # -------------------------------------------------------------
    active_project_ids = set(str(pid) for pid in Project.objects.values_list("id", flat=True))
    active_excel_names = set(ExcelFile.objects.exclude(file="").values_list("file", flat=True))
    active_meta_names = set(Project.objects.exclude(metadata_file="").values_list("metadata_file", flat=True))

    orphan_targets = [
        ("parsed", parsed_dir, lambda p: p.stem in active_project_ids),
        ("workbooks", workbooks_dir, lambda p: f"workbooks/{p.name}" in active_excel_names or p.name in active_excel_names),
        ("metadata", metadata_dir, lambda p: f"metadata/{p.name}" in active_meta_names or p.name in active_meta_names),
    ]

    for category, dir_path, is_active_check in orphan_targets:
        if not dir_path.exists():
            continue
        try:
            for item in dir_path.iterdir():
                if not item.is_file():
                    continue
                try:
                    stat = item.stat()
                except OSError:
                    continue

                is_stale_by_age = (retention_hours <= 0) or (stat.st_mtime < cutoff_epoch)
                is_active = is_active_check(item)

                # If the file is not tied to any active project, OR if it has surpassed the retention cutoff
                if not is_active or is_stale_by_age:
                    f_size = stat.st_size
                    if not dry_run:
                        try:
                            item.unlink()
                            files_deleted += 1
                            bytes_freed += f_size
                            logger.info("Purged %s file: %s (%.2f KB)", category, item.name, f_size / 1024)
                        except OSError as e:
                            logger.warning("Could not unlink %s: %s", item, e)
                    else:
                        files_deleted += 1
                        bytes_freed += f_size
        except OSError as e:
            logger.warning("Error reading directory %s: %s", dir_path, e)

    mb_freed = round(bytes_freed / (1024 * 1024), 2)
    logger.info(
        "Media cleanup finished (dry_run=%s): %d projects, %d files, %.2f MB freed.",
        dry_run, projects_deleted, files_deleted, mb_freed
    )

    return {
        "cutoff_time": cutoff_dt.isoformat(),
        "retention_hours": retention_hours,
        "projects_deleted": projects_deleted,
        "files_deleted": files_deleted,
        "bytes_freed": bytes_freed,
        "mb_freed": mb_freed,
        "dry_run": dry_run,
        "details": details,
    }


def get_storage_stats() -> Dict[str, Any]:
    """
    Get current storage breakdown of the media directory and database objects.
    """
    from agent.models import Project, ExcelFile

    media_root = Path(settings.MEDIA_ROOT)
    categories = ["workbooks", "parsed", "metadata"]
    cat_stats = {}
    total_bytes = 0
    total_files = 0

    for cat in categories:
        d = media_root / cat
        c_count = 0
        c_bytes = 0
        if d.exists() and d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    c_count += 1
                    c_bytes += _get_file_size_safe(f)
        total_files += c_count
        total_bytes += c_bytes
        cat_stats[cat] = {
            "count": c_count,
            "bytes": c_bytes,
            "size_mb": round(c_bytes / (1024 * 1024), 2),
        }

    return {
        "total_files": total_files,
        "total_size_bytes": total_bytes,
        "total_size_mb": round(total_bytes / (1024 * 1024), 2),
        "active_projects": Project.objects.count(),
        "active_workbooks": ExcelFile.objects.count(),
        "retention_hours": getattr(settings, "MEDIA_RETENTION_HOURS", 12),
        "categories": cat_stats,
    }


def trigger_background_cleanup_if_due(interval_minutes: Optional[int] = None) -> bool:
    """
    Lightweight, thread-safe method to run cleanup in a background thread if
    the specified interval (default: settings.MEDIA_CLEANUP_INTERVAL_MINUTES or 30 mins) has elapsed.
    """
    global _last_cleanup_time

    if interval_minutes is None:
        interval_minutes = getattr(settings, "MEDIA_CLEANUP_INTERVAL_MINUTES", 30)

    interval_seconds = interval_minutes * 60
    current_time = time.time()

    with _cleanup_lock:
        if current_time - _last_cleanup_time < interval_seconds:
            return False
        _last_cleanup_time = current_time

    # Run in background daemon thread so HTTP response is not blocked
    t = threading.Thread(
        target=cleanup_expired_sessions,
        name="RequestTriggeredCleanup",
        daemon=True,
    )
    t.start()
    return True
