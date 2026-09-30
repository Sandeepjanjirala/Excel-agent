import os
import sys
from django.apps import AppConfig


class AgentConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'agent'

    def ready(self):
        # 1. Connect model deletion signal handlers
        from . import signals  # noqa: F401

        # 2. Start background cleanup daemon if enabled
        from django.conf import settings
        if getattr(settings, "ENABLE_BACKGROUND_CLEANUP", True):
            # Avoid running during management commands like migrate, makemigrations, test
            commands_to_skip = {"migrate", "makemigrations", "test", "cleanup_media", "collectstatic", "shell"}
            running_skip_command = any(cmd in sys.argv for cmd in commands_to_skip)

            # Under runserver, Django spawns two processes (parent watcher and reloader child).
            # RUN_MAIN is "true" in the child worker process that actually serves traffic.
            is_runserver = any("runserver" in arg for arg in sys.argv)
            if not running_skip_command and (not is_runserver or os.environ.get("RUN_MAIN") == "true"):
                from .cleanup_daemon import start_cleanup_daemon
                start_cleanup_daemon()
