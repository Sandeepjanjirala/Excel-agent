"""
Generic Excel parsing.

Handles the common real-world case where a sheet has one or more
"banner" / title rows above the real header row (as in the school
marks-and-ranks report). Falls back gracefully to a plain header=0
read when the file is a normal, simple table.

Also handles a *batch* of files (any number, not assumed to share a
schema) by giving every sheet, across every file, a unique, safe
Python variable name so the code-gen step and the sandbox agree on
exactly the same names.
"""
from __future__ import annotations

import re
import pandas as pd
from dataclasses import dataclass, field


SCAN_ROWS = 15  # how many leading rows to inspect when hunting for the header
MAX_COLS_SHOWN = 60  # per-sheet column cap in the schema text sent to the LLM


def _detect_header_row(raw: pd.DataFrame) -> int:
    """
    Look at the first SCAN_ROWS rows of a header=None read and guess which
    one is the *real* column-header row.

    Heuristic: a header row is (a) almost completely filled across the
    sheet's width and (b) every filled cell is text (no numbers yet -
    the numbers start on the first data row below it). Banner/title rows
    are filled in only sparsely, and data rows mix text/numbers.
    """
    n_rows = min(SCAN_ROWS, len(raw))
    n_cols = raw.shape[1]
    best_row = 0
    best_score = -1

    for r in range(n_rows):
        row = raw.iloc[r]
        non_null = row.notna().sum()
        if non_null == 0:
            continue
        all_text = all(isinstance(v, str) for v in row.dropna())
        fill_ratio = non_null / max(n_cols, 1)

        if all_text and fill_ratio >= 0.5:
            score = non_null  # prefer the widest fully-text row
            if score > best_score:  # strictly greater: earliest row wins ties
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


@dataclass
class ParsedWorkbook:
    file_path: str
    sheets: dict          # sheet_name -> SheetInfo
    dataframes: dict      # sheet_name -> pd.DataFrame (raw, NOT yet var-named)


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
            vals = [str(v) for v in raw.iloc[r].dropna().tolist()]
            if vals:
                banner_text.append(" ".join(vals))

        df = xls.parse(sheet_name, header=header_idx)
        df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")
        df.columns = [str(c).strip() for c in df.columns]

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
    lines.append("Columns:")
    lines.extend(f"  - {c} (dtype: {info.dtypes.get(c, 'object')})" for c in cols)
    if info.n_cols > max_cols_shown:
        lines.append(f"  ... and {info.n_cols - max_cols_shown} more columns")
    lines.append(f"Sample rows: {info.sample_rows[:2]}")
    return lines


def schema_summary_text(parsed: ParsedWorkbook, max_cols_shown: int = MAX_COLS_SHOWN) -> str:
    """Render a compact, LLM-friendly description of one workbook's schema
    (all its sheets), with no variable names attached - used for the
    single-file preview shown right after upload."""
    lines = []
    for name, info in parsed.sheets.items():
        lines.extend(_sheet_schema_block(name, info, max_cols_shown))
        lines.append("")
    return "\n".join(lines)


def build_batch(parsed_files: list[tuple[str, ParsedWorkbook]]):
    """
    Given [(original_filename, ParsedWorkbook), ...] for an entire uploaded
    batch (any number of files, NOT assumed to share a schema), produce:
      - var_map: flat dict of safe_variable_name -> DataFrame, ready for the
        sandbox (one entry per sheet, across all files)
      - schema_text: one combined, LLM-friendly document that shows, for
        every file, every sheet's exact pandas variable name next to its
        columns - so code-gen and the sandbox always agree on names.

    Naming: <file_stub>__<sheet_stub>, de-duplicated with a numeric suffix
    on collision (two files with the same name, two sheets both called
    "Sheet1", etc). If a file has exactly one sheet, the sheet suffix is
    dropped for readability unless that would itself collide.
    """
    var_map: dict[str, pd.DataFrame] = {}
    file_vars: list[list[str]] = []  # positional, parallel to parsed_files - filenames may repeat
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
        "own columns before writing code that touches more than one.",
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
