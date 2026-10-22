# VaiTestPipe — Backend (FastAPI)

Crawls a URL with Playwright, generates test cases with Groq (default model: openai/gpt-oss-120b; Groq no longer serves Groq-hosted LLM chat models), lets you add
manual tests, runs everything in Playwright, and exports reports.

## Setup (Windows / PowerShell)

```powershell
cd backend
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env      # then put your GROQ_API_KEY in .env
```

macOS/Linux: `python3 -m venv .venv && source .venv/bin/activate`, same after that.

## Run

```powershell
uvicorn app.main:app --reload --port 8000
```

Interactive API docs: http://localhost:8000/docs

> On Windows, `--reload` sometimes hangs after a file change. If requests stop
> picking up your edits, stop the server with Ctrl+C and start it again.

## How it works

| Module | Role |
|---|---|
| `app/scraper.py` | Playwright opens the URL, extracts inputs/buttons/links/selects/forms with a stable selector for each, and takes a full-page screenshot |
| `app/groq_client.py` | Sends the page snapshot to Groq and gets back test cases in JSON mode. Also turns plain-English manual steps into executable steps |
| `app/executor.py` | Runs each test's steps in its own browser context and records Pass / Fail / Error, logs, and a screenshot on failure |
| `app/storage.py` | JSON storage in `data/` (`test_cases.json`, `test_runs.json`, `last_crawl.json`) |
| `app/reports.py` | JSON, CSV, and HTML report exporters |

**Executable steps.** Each test case has human-readable fields (title, action steps,
expected outcome) plus a `steps` list written in a fixed vocabulary: `goto`, `click`,
`fill`, `press`, `select`, `check`, `hover`, `wait`, `expect_visible`,
`expect_hidden`, `expect_text`, `expect_value`, `expect_title`, `expect_url`. The
executor interprets these steps and never executes model output as code. Steps the
model returns outside this vocabulary are dropped.

**Statuses.**
- **Pass**: every step ran and every assertion held.
- **Fail**: an `expect_*` assertion didn't hold.
- **Error**: the test couldn't run (element not found, navigation failed, or no steps).

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | Also reports whether Groq is configured |
| POST | `/api/crawl` | `{"url": "...", "max_tests": 8, "replace_existing_ai": true}` |
| GET | `/api/test-cases` | All test cases (AI + manual) |
| POST | `/api/test-cases/add` | `{"title", "action_steps": [..], "expected_result", "target_url"?, "steps"?}` |
| PUT | `/api/test-cases/{id}` | Partial update (edit steps, enable/disable, ...) |
| DELETE | `/api/test-cases/{id}` | |
| POST | `/api/run-tests` | Optional `{"test_ids": [..]}`. Returns 409 if a run is already in progress |
| GET | `/api/runs`, `/api/runs/latest` | Run history (last 20 runs) |
| GET | `/api/reports/download?format=json\|csv\|html&run_id=` | Latest run by default |
| GET | `/static/screenshots/...` | Crawl and failure screenshots |

Manual tests without `steps` get compiled by Groq. If Groq is unavailable, the test is
saved with no steps and reports **Error** until you add steps with `PUT`.

## Quick try

```powershell
curl.exe -X POST http://localhost:8000/api/crawl -H "content-type: application/json" -d '{\"url\":\"https://example.com\"}'
curl.exe -X POST http://localhost:8000/api/run-tests
curl.exe -OJ "http://localhost:8000/api/reports/download?format=html"
```
