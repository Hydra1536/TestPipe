# VaiTestPipe

AI-powered automated testing pipeline. Enter a URL, and the app crawls it with Playwright,
has an LLM on Groq write test cases, lets you add manual tests, runs everything in a
headless browser, and shows the results with downloadable reports. See [PRD.md](PRD.md).

```
frontend/  Next.js 16 + Tailwind v4 dashboard   → http://localhost:3000
backend/   FastAPI + Playwright + Groq           → http://localhost:8000 (docs at /docs)
```

## Quick start (two terminals)

```powershell
# 1. backend
cd backend
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt; python -m playwright install chromium
copy .env.example .env        # set GROQ_API_KEY
uvicorn app.main:app --reload --port 8000

# 2. frontend
cd frontend
npm install; copy .env.local.example .env.local
npm run dev
```

See [backend/README.md](backend/README.md) for how it works and the API reference, and
[frontend/README.md](frontend/README.md) for the UI structure.

## Known limitations (MVP)
- Sites behind bot protection (Cloudflare, etc.) may serve a challenge page. The crawl
  detects this and reports it instead of generating tests for the challenge page.
- The AI only sees the page structure (elements, text, and selectors), not client-side
  behaviour. Tests for things like validation messages work best as manual tests.
- Test runs are synchronous, so the "Run Tests" request stays open until every test finishes.
