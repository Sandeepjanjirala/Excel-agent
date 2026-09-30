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
from .services.rules_extractor import extract_rules_text
from .services.graph import answer_question
from .services.cleanup import trigger_background_cleanup_if_due

PARSED_DIR = Path(settings.MEDIA_ROOT) / "parsed"
PARSED_DIR.mkdir(exist_ok=True)

MAX_FILES_PER_UPLOAD = 25  # generous headroom above the ~15 files you're using


def index(request):
    response = render(request, "agent/index.html")
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@csrf_exempt
@require_http_methods(["POST"])
def upload_project(request):
    """
    Accepts any number of Excel files (field name "excel_files", repeated)
    plus one optional shared metadata/business-rules file (field name
    "metadata_file", supporting .xlsx, .xls, .pdf, .png, .jpg, .md, .txt, .csv).
    """
    excel_files = request.FILES.getlist("excel_files")
    metadata_file = request.FILES.get("metadata_file")
    user_api_key = request.POST.get("gemini_api_key") or None

    # Trigger lazy background cleanup if due to keep media storage lean
    trigger_background_cleanup_if_due()

    if not excel_files:
        return JsonResponse({"error": "No Excel files provided."}, status=400)
    if len(excel_files) > MAX_FILES_PER_UPLOAD:
        return JsonResponse(
            {"error": f"Too many files ({len(excel_files)}). Max is {MAX_FILES_PER_UPLOAD}."},
            status=400,
        )

    try:
        project = Project.objects.create(metadata_file=metadata_file)

        if metadata_file:
            project.metadata_file.open("rb")
            try:
                project.business_rules_text = extract_rules_text(
                    project.metadata_file,
                    filename=metadata_file.name,
                    api_key=user_api_key,
                )
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

        files_info = []
        total_rows = 0
        total_sheets = 0
        for (orig_name, parsed), ef, v_list in zip(parsed_for_batch, file_records, file_vars):
            sheets_data = []
            for s_name, s_info in parsed.sheets.items():
                total_sheets += 1
                total_rows += int(s_info.n_rows)
                col_names = [str(c) for c in s_info.columns]
                sheets_data.append({
                    "sheet_name": str(s_name),
                    "variable": v_list[0] if len(v_list) == 1 else f"{orig_name}__{s_name}",
                    "rows": int(s_info.n_rows),
                    "cols": int(s_info.n_cols),
                    "columns": col_names[:50],
                    "total_columns": len(col_names),
                    "banner_detected": bool(s_info.header_row_index > 0),
                })
            files_info.append({
                "name": str(orig_name),
                "variables": v_list,
                "sheets": sheets_data,
            })

        response = {
            "project_id": str(project.id),
            "files": files_info,
            "variables": list(var_map.keys()),
            "total_files": len(files_info),
            "total_sheets": total_sheets,
            "total_rows": total_rows,
            "schema_preview": combined_schema,
            "has_metadata": bool(metadata_file),
            "metadata_name": metadata_file.name if metadata_file else None,
        }
        if errors:
            response["warnings"] = errors

        return JsonResponse(response)
    except Exception as e:
        return JsonResponse({"error": f"Error processing Excel files: {e}"}, status=400)


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

    # Fetch recent conversation history for this project BEFORE saving current question
    past_messages = ChatMessage.objects.filter(project=project).order_by("-created_at")[:6]
    history_lines = []
    for m in reversed(list(past_messages)):
        history_lines.append(f"{m.role.upper()}: {m.content}")
        if m.code_used:
            history_lines.append(f"[Code used previously]:\n{m.code_used}")
    chat_history_text = "\n".join(history_lines) if history_lines else None

    ChatMessage.objects.create(project=project, role="user", content=question)

    try:
        result = answer_question(
            question=question,
            schema_text=project.schema_text,
            business_rules_text=project.business_rules_text,
            dataframes=var_map,
            chat_history_text=chat_history_text,
            api_key=user_api_key,
        )
    except Exception as e:
        return JsonResponse({"error": f"Agent error: {e}"}, status=500)

    ChatMessage.objects.create(
        project=project, role="agent", content=result["answer"], code_used=result.get("code"),
    )

    return JsonResponse(result)


@csrf_exempt
@require_http_methods(["POST"])
def delete_project(request):
    """
    Explicitly delete a project and all associated media files immediately.
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON body."}, status=400)

    project_id = body.get("project_id")
    if not project_id:
        return JsonResponse({"error": "project_id is required."}, status=400)

    from .services.cleanup import cleanup_project
    deleted = cleanup_project(project_id)
    return JsonResponse({"status": "ok", "deleted": deleted, "project_id": str(project_id)})


@require_http_methods(["GET"])
def storage_stats_view(request):
    """
    Inspect current disk usage and file counts for the media directory.
    """
    from .services.cleanup import get_storage_stats
    return JsonResponse(get_storage_stats())
