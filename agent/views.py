import json
import pickle
from pathlib import Path

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Project, ExcelFile, ChatMessage
from .services.excel_parser import parse_workbook, schema_summary_text, build_batch
from .services.graph import answer_question

PARSED_DIR = Path(settings.MEDIA_ROOT) / "parsed"
PARSED_DIR.mkdir(exist_ok=True)

MAX_FILES_PER_UPLOAD = 25  # generous headroom above the ~15 files you're using


def index(request):
    return render(request, "agent/index.html")


@csrf_exempt
@require_http_methods(["POST"])
def upload_project(request):
    """
    Accepts any number of Excel files (field name "excel_files", repeated)
    plus one optional shared metadata/business-rules file (field name
    "metadata_file", a .md/.txt). Files are NOT assumed to share a schema.
    """
    excel_files = request.FILES.getlist("excel_files")
    metadata_file = request.FILES.get("metadata_file")  # optional .md

    if not excel_files:
        return JsonResponse({"error": "No Excel files provided."}, status=400)
    if len(excel_files) > MAX_FILES_PER_UPLOAD:
        return JsonResponse(
            {"error": f"Too many files ({len(excel_files)}). Max is {MAX_FILES_PER_UPLOAD}."},
            status=400,
        )

    project = Project.objects.create(metadata_file=metadata_file)

    if metadata_file:
        project.metadata_file.open("rb")
        try:
            project.business_rules_text = project.metadata_file.read().decode("utf-8", errors="ignore")
        finally:
            project.metadata_file.close()
        project.save()

    parsed_for_batch = []  # [(original_name, ParsedWorkbook), ...]
    file_records = []
    errors = []

    for uploaded in excel_files:
        ef = ExcelFile.objects.create(project=project, file=uploaded, original_name=uploaded.name)
        try:
            parsed = parse_workbook(ef.file.path)
        except Exception as e:
            errors.append(f"{uploaded.name}: could not read file ({e})")
            ef.delete()
            continue

        ef.schema_text = schema_summary_text(parsed)
        ef.save()
        parsed_for_batch.append((uploaded.name, parsed))
        file_records.append(ef)

    if not parsed_for_batch:
        project.delete()
        return JsonResponse({"error": "None of the uploaded files could be read.", "details": errors}, status=400)

    var_map, combined_schema, file_vars = build_batch(parsed_for_batch)

    with open(PARSED_DIR / f"{project.id}.pkl", "wb") as f:
        pickle.dump(var_map, f)

    project.schema_text = combined_schema
    project.save()

    response = {
        "project_id": str(project.id),
        "files": [
            {"name": ef.original_name, "variables": vars_for_file}
            for ef, vars_for_file in zip(file_records, file_vars)
        ],
        "variables": list(var_map.keys()),
        "schema_preview": combined_schema[:4000],
        "has_metadata": bool(metadata_file),
    }
    if errors:
        response["warnings"] = errors

    return JsonResponse(response)


@csrf_exempt
@require_http_methods(["POST"])
def ask_question(request):
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    project_id = body.get("project_id")
    question = (body.get("question") or "").strip()
    user_api_key = (body.get("gemini_api_key") or "").strip() or None
    if not project_id or not question:
        return JsonResponse({"error": "project_id and question are required."}, status=400)

    project = get_object_or_404(Project, id=project_id)

    pkl_path = PARSED_DIR / f"{project.id}.pkl"
    if not pkl_path.exists():
        return JsonResponse({"error": "Uploaded data not found. Please re-upload."}, status=404)
    with open(pkl_path, "rb") as f:
        var_map = pickle.load(f)

    ChatMessage.objects.create(project=project, role="user", content=question)

    try:
        result = answer_question(
            question=question,
            schema_text=project.schema_text,
            business_rules_text=project.business_rules_text,
            dataframes=var_map,
            api_key=user_api_key,
        )
    except Exception as e:
        return JsonResponse({"error": f"Agent error: {e}"}, status=500)

    ChatMessage.objects.create(
        project=project, role="agent", content=result["answer"], code_used=result.get("code"),
    )

    return JsonResponse(result)
