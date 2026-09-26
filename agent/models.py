import uuid
from django.db import models


class Project(models.Model):
    """
    One chat session. Holds an arbitrary number of uploaded Excel files
    (the set is not fixed - could be 1 file or 15, and they are not
    assumed to share a schema) plus one optional shared Markdown file
    of metadata / business rules that applies loosely across the batch.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    metadata_file = models.FileField(upload_to="metadata/", blank=True, null=True)
    # Raw text of the .md file, if one was uploaded. Treated as *supplementary*
    # context for the whole batch, not a guaranteed per-file spec - a single
    # shared note may not describe every uploaded file's columns exactly.
    business_rules_text = models.TextField(blank=True)
    # The full combined schema (every file, every sheet, with its exact
    # pandas variable name) built once at upload time by
    # excel_parser.build_batch(). Persisted here - not just cached in
    # memory - so a server restart doesn't desync the variable names shown
    # to the LLM from the ones actually pickled on disk.
    schema_text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Project {self.id} ({self.files.count()} file(s))"


class ExcelFile(models.Model):
    """A single uploaded workbook belonging to a Project."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="files")
    file = models.FileField(upload_to="workbooks/")
    original_name = models.CharField(max_length=255)
    # Per-file schema text (sheets/columns/dtypes), rendered at upload time.
    schema_text = models.TextField(blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]

    def __str__(self):
        return self.original_name


class ChatMessage(models.Model):
    ROLE_CHOICES = [("user", "user"), ("agent", "agent")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    content = models.TextField()
    code_used = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
