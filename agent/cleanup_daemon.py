"""
Background Cleanup Daemon.

Runs periodically in a background daemon thread while the Django server is up.
Automatically purges media and project sessions that exceed the 12-hour retention policy.
"""
import logging
import threading
from django.conf import settings

logger = logging.getLogger("agent.cleanup")

_daemon_thread: threading.Thread | None = None
_stop_event = threading.Event()
_daemon_lock = threading.Lock()


def _cleanup_worker():
    """Worker function executed inside the background daemon thread."""
    from agent.services.cleanup import cleanup_expired_sessions

    # Initial grace period (5s) to allow migrations and initial requests to settle
    if _stop_event.wait(timeout=5):
        return

    logger.info("Media cleanup daemon worker started (retention: %dh).", getattr(settings, "MEDIA_RETENTION_HOURS", 12))

    while not _stop_event.is_set():
        try:
            cleanup_expired_sessions()
        except Exception as e:
            logger.error("Error during periodic media cleanup: %s", e, exc_info=True)

        interval_minutes = getattr(settings, "MEDIA_CLEANUP_INTERVAL_MINUTES", 30)
        interval_seconds = max(60, interval_minutes * 60)

        # Wait for the next interval or until stop is requested
        if _stop_event.wait(timeout=interval_seconds):
            break

    logger.info("Media cleanup daemon worker stopped.")


def start_cleanup_daemon():
    """Start the periodic cleanup daemon if not already running."""
    global _daemon_thread

    with _daemon_lock:
        if _daemon_thread is not None and _daemon_thread.is_alive():
            return

        _stop_event.clear()
        _daemon_thread = threading.Thread(
            target=_cleanup_worker,
            name="MediaCleanupDaemon",
            daemon=True,
        )
        _daemon_thread.start()
        logger.debug("Started MediaCleanupDaemon thread.")


def stop_cleanup_daemon():
    """Stop the periodic cleanup daemon."""
    global _daemon_thread

    with _daemon_lock:
        if _daemon_thread is not None and _daemon_thread.is_alive():
            _stop_event.set()
            _daemon_thread.join(timeout=2)
            _daemon_thread = None
            logger.debug("Stopped MediaCleanupDaemon thread.")
