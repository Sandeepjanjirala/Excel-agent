"""
Generic Excel parsing with smart header detection and rich schema profiling.

Handles:
- Header rows with banner / title rows above the real header
- Number-based header names (years, codes, etc.)
- Multi-column sheets (up to 500+ columns shown without truncation)
- Whitespace stripping on cell strings to prevent query mismatch
- Column name cleanup and de-duplication
- Rich data profiling (distinct values for categories, numeric ranges)
  so Gemini knows exactly what values exist in the columns.
"""
from __future__ import annotations

import re
import pandas as pd
import numpy as np
from dataclasses import dataclass, field


SCAN_ROWS = 20  # how many leading rows to inspect when hunting for the header
MAX_COLS_SHOWN = 500  # generous cap so multi-column sheets (150+ cols) aren't truncated


def _detect_header_row(raw: pd.DataFrame) -> int:
    """
    Look at the first SCAN_ROWS rows of a header=None read and guess which
    one is the *real* column-header row.

    Heuristic: A real header row:
    (a) has high non-null fill across the sheet's width
    (b) has high uniqueness among non-null values (unlike data rows which repeat categories)
    (c) contains short identifiers/labels, not long narrative paragraphs
    (d) is often followed by rows with homogeneous numeric or data values
    """
    n_rows = min(SCAN_ROWS, len(raw))
    n_cols = raw.shape[1]
    best_row = 0
    best_score = -1.0

    for r in range(n_rows):
        row = raw.iloc[r]
        non_null_cells = [v for v in row.dropna() if str(v).strip() != ""]
        non_null_count = len(non_null_cells)
        if non_null_count == 0:
            continue

        # Convert to string representations
        str_vals = [str(v).strip() for v in non_null_cells]
        unique_count = len(set(str_vals))
        uniqueness_ratio = unique_count / max(non_null_count, 1)
        fill_ratio = non_null_count / max(n_cols, 1)

        # Average length of cell values in this row
        avg_len = sum(len(s) for s in str_vals) / max(len(str_vals), 1)

        # Disqualify rows that are just a single merged banner or title
        if non_null_count == 1 and n_cols > 2:
            continue
        # Disqualify rows with very low uniqueness (e.g. repeated data values)
        if uniqueness_ratio < 0.6:
            continue
        # Disqualify rows where cell contents look like long narrative descriptions (> 100 chars avg)
        if avg_len > 100:
            continue

        score = (non_null_count * 2.0) + (uniqueness_ratio * 15.0)

        # Bonus if fill ratio is high
        if fill_ratio >= 0.5:
            score += 10.0

        # Check if row looks like column names rather than all numbers
        numeric_count = 0
        for s in str_vals:
            try:
                float(s)
                numeric_count += 1
            except ValueError:
                pass
        text_ratio = 1.0 - (numeric_count / max(len(str_vals), 1))
        score += text_ratio * 10.0

        # Look at the next row (if any) - if next row has numbers or different types, strong indicator
        if r + 1 < len(raw):
            next_row = raw.iloc[r + 1].dropna()
            next_vals = [str(v).strip() for v in next_row if str(v).strip() != ""]
            if next_vals:
                next_numeric = sum(1 for v in next_vals if v.replace(".", "", 1).isdigit())
                if next_numeric > numeric_count:
                    score += 8.0

        if score > best_score:
            best_score = score
            best_row = r

    return best_row if best_score > 0 else 0


@dataclass
class SheetInfo:
    name: str
    header_row_index: int
    n_rows: int
    n_cols: int
    columns: list = field(default_factory=list)
    dtypes: dict = field(default_factory=dict)
    sample_rows: list = field(default_factory=list)
    banner_text: list = field(default_factory=list)
    column_profiles: dict = field(default_factory=dict)


@dataclass
class ParsedWorkbook:
    file_path: str
    sheets: dict          # sheet_name -> SheetInfo
    dataframes: dict      # sheet_name -> pd.DataFrame (cleaned, var-ready)


def _profile_dataframe(df: pd.DataFrame) -> dict:
    """Extract distinct values for categories and ranges for numeric columns."""
    profiles = {}
    total_rows = len(df)

    for col in df.columns:
        series = df[col]
        non_null_count = int(series.notna().sum())

        if pd.api.types.is_numeric_dtype(series):
            clean_s = series.dropna()
            if not clean_s.empty:
                min_val = clean_s.min()
                max_val = clean_s.max()
                if isinstance(min_val, (np.integer, int)):
                    min_val = int(min_val)
                    max_val = int(max_val)
                elif isinstance(min_val, (np.floating, float)):
                    min_val = round(float(min_val), 2)
                    max_val = round(float(max_val), 2)
                profiles[col] = {
                    "kind": "numeric",
                    "min": min_val,
                    "max": max_val,
                    "non_null": f"{non_null_count}/{total_rows}",
                }
            else:
                profiles[col] = {"kind": "numeric", "all_null": True}
        else:
            clean_s = series.dropna().astype(str).str.strip()
            unique_vals = clean_s.unique().tolist()
            unique_count = len(unique_vals)

            if unique_count <= 25:
                profiles[col] = {
                    "kind": "category",
                    "values": unique_vals,
                    "unique_count": unique_count,
                }
            else:
                profiles[col] = {
                    "kind": "text",
                    "unique_count": unique_count,
                    "samples": unique_vals[:5],
                }

    return profiles


def parse_workbook(file_path: str) -> ParsedWorkbook:
    xls = pd.ExcelFile(file_path)
    sheets = {}
    dataframes = {}

    for sheet_name in xls.sheet_names:
        raw = xls.parse(sheet_name, header=None)
        if raw.empty:
            continue

        header_idx = _detect_header_row(raw)

        banner_text = []
        for r in range(header_idx):
            vals = [str(v).strip() for v in raw.iloc[r].dropna().tolist() if str(v).strip()]
            if vals:
                banner_text.append(" ".join(vals))

        df = xls.parse(sheet_name, header=header_idx)
        df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")

        # Clean column names
        cleaned_cols = []
        for i, c in enumerate(df.columns):
            c_str = str(c).strip().replace("\n", " ").replace("\r", " ")
            c_str = re.sub(r"\s+", " ", c_str)
            if not c_str or c_str.lower().startswith("unnamed:"):
                c_str = f"Col_{i+1}"
            cleaned_cols.append(c_str)

        # De-duplicate column names if any repeated
        seen_cols: dict[str, int] = {}
        final_cols = []
        for c in cleaned_cols:
            if c not in seen_cols:
                seen_cols[c] = 1
                final_cols.append(c)
            else:
                seen_cols[c] += 1
                final_cols.append(f"{c}_{seen_cols[c]}")
        df.columns = final_cols

        # Clean string/object cells: strip leading/trailing whitespace
        for col in df.columns:
            if df[col].dtype == "object":
                df[col] = df[col].apply(lambda x: x.strip() if isinstance(x, str) else x)

        column_profiles = _profile_dataframe(df)

        dataframes[sheet_name] = df
        sheets[sheet_name] = SheetInfo(
            name=sheet_name,
            header_row_index=header_idx,
            n_rows=len(df),
            n_cols=len(df.columns),
            columns=list(df.columns),
            dtypes={c: str(t) for c, t in df.dtypes.items()},
            sample_rows=df.head(3).to_dict(orient="records"),
            banner_text=banner_text,
            column_profiles=column_profiles,
        )

    return ParsedWorkbook(file_path=file_path, sheets=sheets, dataframes=dataframes)


def _slug(text: str) -> str:
    """Turn arbitrary text into a safe, lowercase python-identifier fragment."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", text).strip("_").lower()
    if not cleaned:
        cleaned = "sheet"
    if cleaned[0].isdigit():
        cleaned = "f_" + cleaned
    return cleaned


def _sheet_schema_block(name: str, info: SheetInfo, max_cols_shown: int = MAX_COLS_SHOWN) -> list[str]:
    lines = [f"### Sheet: {name}"]
    if info.banner_text:
        lines.append(f"(Banner/title rows above header: {' | '.join(info.banner_text)})")
    lines.append(f"Rows: {info.n_rows}, Columns: {info.n_cols}")

    cols = info.columns[:max_cols_shown]
    lines.append("Columns & Data Profiles:")
    for c in cols:
        prof = info.column_profiles.get(c, {})
        dtype_str = info.dtypes.get(c, "object")
        kind = prof.get("kind")

        if kind == "category":
            vals = prof.get("values", [])
            lines.append(f"  - `{c}` ({dtype_str}) -> distinct values: {vals}")
        elif kind == "text":
            u_count = prof.get("unique_count", 0)
            samples = prof.get("samples", [])
            lines.append(f"  - `{c}` ({dtype_str}) -> {u_count} unique values, sample values: {samples}")
        elif kind == "numeric":
            min_v = prof.get("min")
            max_v = prof.get("max")
            nn = prof.get("non_null", "")
            lines.append(f"  - `{c}` ({dtype_str}) -> range [{min_v} to {max_v}], non-null: {nn}")
        else:
            lines.append(f"  - `{c}` ({dtype_str})")

    if info.n_cols > max_cols_shown:
        lines.append(f"  ... and {info.n_cols - max_cols_shown} more columns")

    lines.append(f"Sample rows: {info.sample_rows[:2]}")
    return lines


def schema_summary_text(parsed: ParsedWorkbook, max_cols_shown: int = MAX_COLS_SHOWN) -> str:
    """Render a compact, LLM-friendly description of one workbook's schema."""
    lines = []
    for name, info in parsed.sheets.items():
        lines.extend(_sheet_schema_block(name, info, max_cols_shown))
        lines.append("")
    return "\n".join(lines)


def build_batch(parsed_files: list[tuple[str, ParsedWorkbook]]):
    """
    Given [(original_filename, ParsedWorkbook), ...] for an entire uploaded
    batch, produce:
      - var_map: flat dict of safe_variable_name -> DataFrame
      - schema_text: one combined, LLM-friendly document
      - file_vars: variable names associated with each file
    """
    var_map: dict[str, pd.DataFrame] = {}
    file_vars: list[list[str]] = []
    used_names: set[str] = set()

    def unique(name: str) -> str:
        if name not in used_names:
            used_names.add(name)
            return name
        i = 2
        while f"{name}_{i}" in used_names:
            i += 1
        used_names.add(f"{name}_{i}")
        return f"{name}_{i}"

    doc_lines = [
        f"This batch contains {len(parsed_files)} uploaded Excel file(s). "
        "They are NOT assumed to share the same schema - check each file's "
        "own columns and profiles before writing code that touches more than one.",
        "",
    ]

    for original_name, parsed in parsed_files:
        file_stub = _slug(original_name.rsplit(".", 1)[0])
        single_sheet = len(parsed.dataframes) == 1
        doc_lines.append(f"## File: {original_name}")
        this_file_vars = []

        for sheet_name, df in parsed.dataframes.items():
            candidate = file_stub if single_sheet else f"{file_stub}__{_slug(sheet_name)}"
            var_name = unique(candidate)
            var_map[var_name] = df
            this_file_vars.append(var_name)

            info = parsed.sheets[sheet_name]
            doc_lines.append(f"(pandas variable: `{var_name}`)")
            doc_lines.extend(_sheet_schema_block(sheet_name, info))
            doc_lines.append("")

        file_vars.append(this_file_vars)

    schema_text = "\n".join(doc_lines)
    return var_map, schema_text, file_vars
