"""
Wrapper around the Gemini API (google-genai SDK) with multi-model fallback,
rich conversational context, intelligent data-analysis prompting, and reflection support.
"""
from __future__ import annotations

import time
from django.conf import settings
from google import genai

_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
    "overloaded", "rate limit", "quota", "not found", "404",
)

_RETRIES_PER_MODEL = 2
_BACKOFF_SECONDS = (1, 3)


class GeminiUnavailableError(RuntimeError):
    """Raised when every model in the fallback chain failed."""


def _model_chain() -> list[str]:
    configured = [settings.GEMINI_MODEL] + list(settings.GEMINI_FALLBACK_MODELS)
    # Include standard stable fallbacks to ensure high availability
    standard_fallbacks = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    chain = configured + standard_fallbacks

    seen = set()
    ordered = []
    for m in chain:
        if m and m not in seen:
            seen.add(m)
            ordered.append(m)
    return ordered


def _is_transient(error: Exception) -> bool:
    msg = str(error)
    return any(marker.lower() in msg.lower() for marker in _TRANSIENT_MARKERS)


def _get_client(api_key: str | None):
    key = api_key or settings.GEMINI_API_KEY
    if not key:
        raise RuntimeError(
            "No Gemini API key available. Either set GEMINI_API_KEY in your "
            "server's .env file, or enter your own key in the app (top of the page)."
        )
    return genai.Client(api_key=key)


def _generate_with_fallback(prompt: str, api_key: str | None) -> str:
    client = _get_client(api_key)
    last_error: Exception | None = None

    for model in _model_chain():
        for attempt in range(_RETRIES_PER_MODEL):
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                if response and response.text:
                    return response.text
            except Exception as e:
                last_error = e
                if not _is_transient(e):
                    break
                if attempt < _RETRIES_PER_MODEL - 1:
                    time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])

    raise GeminiUnavailableError(
        f"All configured Gemini models are currently unavailable. Last error: {last_error}"
    )


CODEGEN_SYSTEM_PROMPT = """You are a senior data analyst and expert pandas programmer.
Your job is to write short, 100% correct, and robust pandas code to answer questions about uploaded Excel spreadsheets.

CRITICAL RULES FOR 100% ACCURACY:
1. OUTPUT FORMAT:
   - Output ONLY a python code block (```python ... ```). No commentary before or after.
   - Do not import `os`, `sys`, or unsafe modules. Standard data modules (`pd`, `np`, `re`, `math`, `datetime`) are already imported and available.
   - The dataframes are pre-loaded into local variables matching the exact names given in the schema below. If only 1 sheet exists, it is also available as `df`.
   - You MUST assign the final computed result to a variable named `answer`.

2. STRING MATCHING & CASE INSENSITIVITY:
   - Real-world Excel sheets frequently have inconsistent casing or trailing spaces.
   - When filtering or searching on text columns (e.g. branch names, zones, AGM/RI names, statuses, categories):
     ALWAYS strip whitespace and use case-insensitive matching:
     Example: `df[df['Branch'].astype(str).str.strip().str.lower() == 'adibatla'.lower()]`
     Or for substring: `df[df['Branch'].astype(str).str.contains('adibatla', case=False, na=False)]`
   - NEVER do strict exact case comparisons like `df['Branch'] == 'ADIBATLA'` without `.str.strip().str.lower()`.

3. DATA PROFILING & DISTINCT VALUES:
   - Inspect the 'Columns & Data Profiles' in the schema below! For categorical columns, the exact distinct values are explicitly listed.
   - Match against those exact values (e.g., if asking for "non-ac", check the profile to see if the value is `'Non-AC'` or `'Non - AC'`).

4. MULTI-TABLE JOINS:
   - When combining tables across files, identify the shared entity identifier (such as `'Branch'`).
   - Merge ONLY on the primary entity key (e.g. `pd.merge(table_a, table_b, on='Branch', how='left')`).
   - DO NOT merge on multiple auxiliary columns (e.g. `on=['Branch', 'AGM Name', 'Zone']`) unless strictly required, because minor spelling/formatting differences in auxiliary columns across files will drop valid rows.
   - Prefer `how='left'` or `how='outer'` so records are not silently lost.

5. NUMBER & PERCENTAGE PARSING:
   - Numbers in Excel can sometimes be formatted as strings with currency signs (₹, $), commas (1,000), or percent signs (15%).
   - If computing sums/averages on an object column that contains numbers, convert with:
     `pd.to_numeric(df['col'].astype(str).str.replace(r'[^\d.-]', '', regex=True), errors='coerce')`

6. CONVERSATION CONTEXT & FOLLOW-UPS:
   - If conversation history is provided, resolve pronouns like "they", "those", "which ones", or "sort that" by referring to the previous questions and answers.

7. KEEP IT DETERMINISTIC & CLEAN:
   - No randomness, no plotting, no file writes.
"""


def _extract_code_block(text: str) -> str:
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    block = parts[1]
    if block.startswith("python"):
        block = block[len("python"):]
    elif block.startswith("py"):
        block = block[len("py"):]
    return block.strip()


def generate_pandas_code(
    question: str,
    schema_text: str,
    business_rules_text: str,
    available_vars: list[str],
    chat_history_text: str | None = None,
    previous_error: str | None = None,
    previous_code: str | None = None,
    reflection_feedback: str | None = None,
    api_key: str | None = None,
) -> str:
    prompt_parts = [
        CODEGEN_SYSTEM_PROMPT,
        "",
        f"Available variables: {', '.join(available_vars)}",
        "",
        "Workbook schema:",
        schema_text,
        "",
        "Business rules / column meanings (if provided):",
        business_rules_text or "(none provided)",
        "",
    ]

    if chat_history_text:
        prompt_parts.extend([
            "Recent conversation history:",
            chat_history_text,
            "",
        ])

    prompt_parts.extend([
        f"Current user question: {question}",
    ])

    if reflection_feedback and previous_code:
        prompt_parts.extend([
            "",
            "CRITICAL SELF-CORRECTION FEEDBACK:",
            f"Your previous attempt produced an invalid or empty result: {reflection_feedback}",
            "Previous code:",
            f"```python\n{previous_code}\n```",
            "Please fix the logic, check casing/whitespace (.str.strip().str.lower()), or adjust join keys.",
        ])
    elif previous_error and previous_code:
        prompt_parts.extend([
            "",
            "Your previous attempt threw an error. Fix it:",
            "Previous code:",
            f"```python\n{previous_code}\n```",
            f"Error: {previous_error}",
        ])

    prompt = "\n".join(prompt_parts)
    text = _generate_with_fallback(prompt, api_key)
    return _extract_code_block(text)


def phrase_answer(
    question: str,
    raw_result,
    code_used: str,
    chat_history_text: str | None = None,
    api_key: str | None = None,
) -> str:
    prompt_parts = [
        "A user asked this question about their Excel data:",
        f'"{question}"',
        "",
    ]
    if chat_history_text:
        prompt_parts.extend([
            "Recent conversation history:",
            chat_history_text,
            "",
        ])

    prompt_parts.extend([
        "The following pandas code was executed against the real data and produced this result:",
        "Code:",
        f"```python\n{code_used}\n```",
        f"Computed Result: {raw_result}",
        "",
        "Task: Write a clear, natural-language, professional answer to the user's question using this result.",
        "Rules:",
        "- State the actual numbers/names from the result accurately.",
        "- If the result is a list or table, format it clearly using Markdown tables or bullet points.",
        "- Do not make up any numbers not in the result.",
        "- Do not mention that you ran pandas code or are an AI.",
        "- Keep the answer direct and informative.",
    ])

    prompt = "\n".join(prompt_parts)
    text = _generate_with_fallback(prompt, api_key)
    return text.strip()
