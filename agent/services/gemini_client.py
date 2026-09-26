"""
Thin wrapper around the Gemini API (google-genai SDK).

Two calls are used:
  1. generate_pandas_code() - question + schema + business rules -> a pandas snippet
  2. phrase_answer()        - the raw computed result -> a natural-language reply

Both accept an optional `api_key`. If the frontend supplies one (a user's own
Gemini key, kept in their own browser), it is used only for that request and
never written to the database or logs. If none is supplied, we fall back to
the server's own GEMINI_API_KEY from settings/.env.

Both also retry across a small fallback chain of models
(settings.GEMINI_MODEL, then settings.GEMINI_FALLBACK_MODELS) so a transient
503/429 "model overloaded" error on one model doesn't fail the whole request.
"""
from __future__ import annotations

import time
from django.conf import settings
from google import genai

# Substrings that indicate a *transient* error worth retrying / falling back
# on, as opposed to a real problem (bad prompt, bad API key, model genuinely
# doesn't exist) that retrying won't fix.
_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
    "overloaded", "rate limit", "quota",
)

# Small per-model retry before giving up on that model and moving to the next.
_RETRIES_PER_MODEL = 2
_BACKOFF_SECONDS = (1, 3)


class GeminiUnavailableError(RuntimeError):
    """Raised when every model in the fallback chain failed."""


def _model_chain() -> list[str]:
    chain = [settings.GEMINI_MODEL] + list(settings.GEMINI_FALLBACK_MODELS)
    # de-dupe while preserving order
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
    # A fresh client per call is cheap and lets us support a different key
    # per request safely (no cross-user caching of credentials).
    return genai.Client(api_key=key)


def _generate_with_fallback(prompt: str, api_key: str | None) -> str:
    client = _get_client(api_key)
    last_error: Exception | None = None

    for model in _model_chain():
        for attempt in range(_RETRIES_PER_MODEL):
            try:
                response = client.models.generate_content(model=model, contents=prompt)
                return response.text
            except Exception as e:  # noqa: BLE001 - we re-classify below
                last_error = e
                if not _is_transient(e):
                    break  # don't bother retrying a non-transient error on this model
                if attempt < _RETRIES_PER_MODEL - 1:
                    time.sleep(_BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)])
        # move on to the next model in the chain

    raise GeminiUnavailableError(
        f"All configured Gemini models are currently unavailable. Last error: {last_error}"
    )


CODEGEN_SYSTEM_PROMPT = """You are a data analyst that writes short, correct pandas code
to answer a user's question about a batch of uploaded Excel files.

Rules you MUST follow:
- Output ONLY a python code block. No prose before or after.
- Do not import anything. `pd` (pandas) and `np` (numpy) are already available.
- The dataframe(s) are already available as local variables - one per sheet,
  across every uploaded file (see the schema below, which shows each file's
  sheets and the *exact* pandas variable name for each one). If the whole
  batch is a single sheet, it is also available as `df`.
- The uploaded files are NOT guaranteed to share the same schema. Before
  writing code that combines more than one file's variable (e.g. pd.concat,
  a join, or comparing values across them), check that their columns
  actually match in the schema below. If a question implies "all files" or
  "every branch" but the files have different columns, do the best
  reasonable thing (e.g. use only the columns they have in common, or
  process each file separately and combine the results) rather than
  guessing a column that isn't there.
- Assign your final result to a variable named exactly `answer`.
  `answer` can be a number, a string, a dict, a list, or a small DataFrame/Series.
- Never invent column names - use only the exact column names given in the schema.
- The business rules / metadata note below (if any) was written once for the
  whole batch and may not describe every file exactly - apply it where it
  clearly fits, but don't force it onto a file whose structure disagrees
  with it.
- Keep the code short and deterministic. No randomness, no plotting, no file I/O.
"""


def _extract_code_block(text: str) -> str:
    if "```" not in text:
        return text.strip()
    parts = text.split("```")
    # parts[1] is usually like "python\n<code>"
    block = parts[1]
    if block.startswith("python"):
        block = block[len("python"):]
    return block.strip()


def generate_pandas_code(
    question: str,
    schema_text: str,
    business_rules_text: str,
    available_vars: list[str],
    previous_error: str | None = None,
    previous_code: str | None = None,
    api_key: str | None = None,
) -> str:
    prompt = f"""{CODEGEN_SYSTEM_PROMPT}

Available variables: {', '.join(available_vars)}

Workbook schema:
{schema_text}

Business rules / column meanings (may be blank if none were provided):
{business_rules_text or '(none provided)'}

User question: {question}
"""

    if previous_error and previous_code:
        prompt += f"""

Your previous attempt failed. Fix it.
Previous code:
```python
{previous_code}
```
Error:
{previous_error}
"""

    text = _generate_with_fallback(prompt, api_key)
    return _extract_code_block(text)


def phrase_answer(question: str, raw_result, code_used: str, api_key: str | None = None) -> str:
    prompt = f"""A user asked this question about their Excel data:
"{question}"

The following pandas code was executed against the real data and produced this result:
Code:
```python
{code_used}
```
Result: {raw_result}

Write a short, natural-language answer to the user's question using this result.
State the actual numbers/names from the result. Do not add caveats about being
an AI. Do not repeat the code. 1-3 sentences unless the result is a list/table,
in which case format it clearly.
"""
    text = _generate_with_fallback(prompt, api_key)
    return text.strip()
