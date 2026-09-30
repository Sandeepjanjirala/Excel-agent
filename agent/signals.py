import os
import logging
from pathlib import Path
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.conf import settings
from .models import Project, ExcelFile

logger = logging.getLogger("agent.cleanup")


@receiver(post_delete, sender=Project)
def delete_project_files_on_post_delete(sender, instance, **kwargs):
    """
    Ensure all physical files on disk associated with a Project
    are purged whenever the Project database record is deleted.
    """
    # 1. Delete metadata file if it exists
    if instance.metadata_file:
        try:
            if instance.metadata_file.name:
                file_path = Path(settings.MEDIA_ROOT) / instance.metadata_file.name
                if file_path.is_file():
                    file_path.unlink(missing_ok=True)
                    logger.debug("Deleted metadata file: %s", file_path)
        except Exception as e:
            logger.warning("Error deleting metadata file for project %s: %s", instance.id, e)

    # 2. Delete parsed DataFrame pickle file
    try:
        pkl_path = Path(settings.MEDIA_ROOT) / "parsed" / f"{instance.id}.pkl"
        if pkl_path.is_file():
            pkl_path.unlink(missing_ok=True)
            logger.debug("Deleted parsed pickle file: %s", pkl_path)
    except Exception as e:
        logger.warning("Error deleting parsed pickle for project %s: %s", instance.id, e)


@receiver(post_delete, sender=ExcelFile)
def delete_excel_file_on_post_delete(sender, instance, **kwargs):
    """
    Ensure the raw uploaded workbook is purged from disk
    whenever the ExcelFile record is deleted.
    """
    if instance.file:
        try:
            if instance.file.name:
                file_path = Path(settings.MEDIA_ROOT) / instance.file.name
                if file_path.is_file():
                    file_path.unlink(missing_ok=True)
                    logger.debug("Deleted excel file: %s", file_path)
        except Exception as e:
            logger.warning("Error deleting excel file %s: %s", instance.id, e)
