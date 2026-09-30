"""
Extracts business rules, definitions, or KPI metrics from diverse file formats:
- Excel / CSV spreadsheets (.xlsx, .xls, .csv)
- PDF documents (.pdf) via native text extraction or Gemini multimodal OCR
- Images (.png, .jpg, .jpeg, .webp) via Gemini multimodal OCR
- Markdown / Text (.md, .txt)
"""
from __future__ import annotations

import io
import mimetypes
from pathlib import Path
import pandas as pd
from google.genai import types

from .gemini_client import _get_client, _model_chain


def extract_rules_text(file_obj, filename: str, api_key: str | None = None) -> str:
    """
    Takes an uploaded file-like object and filename, extracts structured
    business rules / metadata text for the agent to reference.
    """
    ext = Path(filename).suffix.lower()
    content = file_obj.read()
    if isinstance(content, str):
        content = content.encode("utf-8", errors="ignore")

    # 1. Plain text and Markdown
    if ext in [".md", ".txt"]:
        return content.decode("utf-8", errors="replace")

    # 2. CSV spreadsheet
    if ext == ".csv":
        try:
            df = pd.read_csv(io.BytesIO(content))
            return (
                f"### Business Rules Table ({filename})\n\n"
                f"Rows: {len(df)}, Columns: {len(df.columns)}\n\n"
                + df.to_markdown(index=False)
            )
        except Exception:
            return content.decode("utf-8", errors="replace")

    # 3. Excel spreadsheet (.xlsx, .xls)
    if ext in [".xlsx", ".xls"]:
        try:
            xls = pd.ExcelFile(io.BytesIO(content))
            blocks = [f"### Business Rules & Reference Tables ({filename})"]
            for sname in xls.sheet_names:
                df = xls.parse(sname)
                df = df.dropna(how="all")
                blocks.append(f"#### Sheet: {sname} ({len(df)} rows, {len(df.columns)} columns)")
                # If table is reasonably sized, render as Markdown table
                if len(df) <= 100 and len(df.columns) <= 25:
                    blocks.append(df.to_markdown(index=False))
                else:
                    # Provide summary plus top 30 rows
                    blocks.append(df.head(30).to_markdown(index=False))
                    blocks.append(f"... ({len(df) - 30} additional rows truncated for brevity)")
            return "\n\n".join(blocks)
        except Exception as e:
            return f"[Warning: Could not fully parse Excel business rules file {filename}: {e}]"

    # 4. PDF Documents (.pdf)
    if ext == ".pdf":
        extracted_text = ""
        # Try pypdf if installed
        try:
            # pyrefly: ignore [missing-import]
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages_text = [page.extract_text() for page in reader.pages if page.extract_text()]
            if pages_text:
                extracted_text = "\n\n".join(pages_text).strip()
        except Exception:
            pass

        # If pypdf didn't extract sufficient text (e.g. scanned PDF), use Gemini Vision/OCR
        if len(extracted_text) < 50:
            try:
                client = _get_client(api_key)
                prompt = (
                    "This document contains business rules, calculation definitions, KPI formulas, "
                    "or classification criteria for a data analysis task. Please extract, summarize, "
                    "and transcribe all rules, tables, and definitions into clear, structured Markdown."
                )
                part = types.Part.from_bytes(data=content, mime_type="application/pdf")
                for model in _model_chain():
                    try:
                        resp = client.models.generate_content(model=model, contents=[part, prompt])
                        if resp and resp.text:
                            return resp.text.strip()
                    except Exception:
                        continue
            except Exception as e:
                if not extracted_text:
                    return f"[Warning: Could not process PDF business rules {filename}: {e}]"

        return extracted_text or f"[Business rules from {filename}]"

    # 5. Images (.png, .jpg, .jpeg, .webp)
    if ext in [".png", ".jpg", ".jpeg", ".webp"]:
        mime, _ = mimetypes.guess_type(filename)
        mime = mime or "image/png"
        try:
            client = _get_client(api_key)
            prompt = (
                "This image contains business rules, KPI formulas, table criteria, or data guidelines. "
                "Please transcribe all text, equations, tables, criteria, and definitions into clear Markdown."
            )
            part = types.Part.from_bytes(data=content, mime_type=mime)
            for model in _model_chain():
                try:
                    resp = client.models.generate_content(model=model, contents=[part, prompt])
                    if resp and resp.text:
                        return resp.text.strip()
                except Exception:
                    continue
        except Exception as e:
            return f"[Warning: Could not process business rules image {filename}: {e}]"

    # Fallback to UTF-8 decode
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return f"[Uploaded business rules reference: {filename}]"
