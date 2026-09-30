# Excel Agent (Django + jQuery + LangGraph + Gemini)

Upload any Excel file, optionally paired with a Markdown file describing your
columns / business rules, and ask calculation or analytical questions about
it in plain English.

## Why this doesn't just "ask Gemini to read the spreadsheet"

Handing raw spreadsheet data to an LLM and asking it to compute an answer
is unreliable for exactly the kind of questions you care about (sums,
ranks, percentages) — the model can misread numbers or silently make
arithmetic mistakes, especially as the sheet gets larger.

Instead, this agent asks Gemini to **write pandas code**, then runs that
code in a locked-down sandbox against the *real* dataframe. The number
you get back was computed by pandas, not guessed by the model. Gemini's
second job is just to phrase the already-computed result in plain English.

## Pipeline (LangGraph)

```
question ─▶ generate_code (Gemini) ─▶ execute (sandbox)
                    ▲                        │
                    └──── retry on error ─────┘  (max 2 retries)
                                               │ success
                                               ▼
                                     phrase_answer (Gemini)
```

If all retries fail, the user gets an honest "couldn't compute this
reliably" message with the last error, rather than a hallucinated number.

## Safety: the sandbox

`agent/services/sandbox.py` runs generated code with:
- An **AST whitelist** — only safe expression/statement types are allowed;
  `import`, `exec`, `eval`, `open`, `__import__`, dunder access, etc. are
  rejected before any code runs.
- A **restricted builtins** dict — only harmless built-ins (`len`, `sum`,
  `sorted`, ...) are available; no `os`, `sys`, `subprocess`.
- **Process isolation** (`multiprocessing`, `fork`) with a timeout, so a
  runaway or hanging snippet can't stall the request.
- The snippet must assign its result to a variable named `answer` — this
  is the only thing pulled back out.

This is a meaningful safety net, not a formal security boundary — for a
public-facing deployment you'd still want to run this in a properly
sandboxed/containerized worker with resource limits, not just in-process.

## Generic by design

The Excel parser (`agent/services/excel_parser.py`) doesn't assume any
fixed schema. It scans the first several rows of each sheet to find the
real header row automatically (so banner/title rows above the header, like
in the sample marks report, are handled without hardcoding "skip 2 rows").
Any `.md`/`.txt` file uploaded alongside the spreadsheet is passed to
Gemini as extra context — column meanings, derived-column formulas,
ranking scope, how to treat missing values, etc. This is exactly how the
"Marks & Ranks" schema you described gets supported: not as hardcoded
logic, but as metadata injected into the prompt for that upload.

## Model fallback and rate limits

The high-demand 503 you hit was Google's, not this app's. `gemini_client.py`
now retries transient errors (503/429/"overloaded"/"quota") across a small
fallback chain, configured in `.env`:

```
GEMINI_MODEL=gemini-3.8-flash
GEMINI_FALLBACK_MODELS=gemini-3.1-flash-lite,gemini-3.5-flash-lite
```

Each model gets up to 2 quick attempts before moving to the next; a
non-transient error (bad prompt, bad key) skips straight to the next model
instead of wasting retries. If every model in the chain fails, the user sees
one clear message instead of a raw stack trace.

## Bring-your-own API key

The page now has an optional "Your Gemini API key" field. If a visitor fills
it in, their questions use *their* key instead of the server's; if left
blank, it falls back to the server's `GEMINI_API_KEY`. This lets you share
the tool with others without giving them your key or paying for their usage.

- The key is stored **only** in that visitor's own browser (`localStorage`)
  and sent only with their own `/api/ask/` requests — it is never written to
  the database, logged, or shown to any other user.
- It does still cross the network to your Django server on every question
  (needed so the server can call Gemini on the visitor's behalf) — run this
  behind HTTPS before sharing it with anyone outside your own machine, or a
  key typed into the page could be sniffed in transit.
- Nothing here makes a *malicious* visitor's key safe from Google's own
  usage tracking, but it does mean two different visitors' keys never mix
  or get billed to each other.



```bash
cd excel_agent
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# then edit .env and paste your real Gemini API key into GEMINI_API_KEY

python manage.py migrate
python manage.py runserver
```

Visit http://127.0.0.1:8000/

## About the API key you shared in chat

Treat that key as compromised — regenerate it in Google AI Studio /
Google Cloud Console before using this project for real. Then put the
new key only in your local `.env` file (already gitignored). Never paste
API keys into a chat, commit them to source control, or embed them in
any front-end/HTML — `.env` + server-side reads (as this project does via
`settings.py` → `os.environ`) is the safe pattern.

## Project layout

```
excel_agent/
  excel_agent_site/        Django project settings/urls
  agent/
    models.py               Workbook + ChatMessage
    views.py                /api/upload/, /api/ask/
    services/
      excel_parser.py       generic header-detection + schema summary
      gemini_client.py      Gemini calls (codegen + answer phrasing)
      sandbox.py             AST-validated restricted pandas executor
      graph.py               LangGraph pipeline wiring it together
    templates/agent/index.html
    static/agent/{css,js}   jQuery chat UI
```

## Ephemeral Storage & 12-Hour Auto-Cleanup Architecture

By design, spreadsheets uploaded to the agent are treated as ephemeral workspace data rather than permanent storage:

- **12-Hour Auto-Expiration**: Uploaded spreadsheets, extracted metadata, parsed DataFrame pickles (`media/parsed/*.pkl`), and database records automatically expire and get purged after 12 hours (configurable via `MEDIA_RETENTION_HOURS` in `.env`).
- **Multi-Pronged Execution Engine**:
  1. **Background Daemon Worker**: A lightweight background daemon thread (`agent/cleanup_daemon.py`) runs periodically (every 30 mins) while the Django server is running to sweep expired data.
  2. **Lazy Request Fallback**: Incoming upload requests trigger a throttled check (`agent/services/cleanup.py`), guaranteeing stale sessions get cleaned even in environments without long-running background threads.
  3. **Instant "New Session" Cleanup**: Clicking **New Session** in the UI immediately notifies `/api/project/delete/` to free disk space immediately without waiting 12 hours.
  4. **Post-Delete Cascade Signals**: Django `post_delete` signals on `Project` and `ExcelFile` ensure deleting database records automatically unlinks all raw `.xlsx`, metadata, and `.pkl` files on disk.
  5. **Orphan File Sweeper**: Automatically discovers and purges any unreferenced files in `media/workbooks`, `media/metadata`, or `media/parsed` that exceed the retention cutoff.
- **Management CLI Command**:
  ```bash
  # Check current disk usage and file breakdown
  python manage.py cleanup_media --stats

  # Run cleanup with default 12-hour threshold
  python manage.py cleanup_media

  # Run cleanup with custom hours or dry-run preview
  python manage.py cleanup_media --hours 6 --dry-run

  # Force purge all media and sessions immediately
  python manage.py cleanup_media --all
  ```

## Known limitations / next steps

- Uploaded workbooks are parsed once and cached as a pickle on disk
  (`media/parsed/<id>.pkl`) for fast repeat questions, and auto-purged after 12 hours.
  For distributed multi-server deployments, swap local disk pickles for Redis or S3 with lifecycle rules.
- CSRF is currently exempted on the two API views for simplicity; before
  any public deployment, wire up Django's CSRF token in the jQuery AJAX
  calls instead of exempting the views.
- No authentication/multi-tenancy yet — any visitor can see any
  `workbook_id`. Add auth + per-user scoping before deploying beyond your
  own machine.
- `ALLOWED_HOSTS = ['*']` and `DEBUG = True` are dev-only settings in
  `excel_agent_site/settings.py` — lock these down for production.
