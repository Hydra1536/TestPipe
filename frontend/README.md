# VaiTestPipe — Frontend (Next.js + Tailwind)

Single-page dashboard for the VaiTestPipe backend.

## Run

```powershell
cd frontend
npm install
copy .env.local.example .env.local   # points at the backend, default http://localhost:8000
npm run dev                          # http://localhost:3000
```

The backend must be running, and its `CORS_ORIGINS` must include this app's origin
(the default `http://localhost:3000` is already set).

## Structure

| Path | What it is |
|---|---|
| `app/page.tsx` | Dashboard: holds all state and calls the API |
| `lib/api.ts`, `lib/types.ts` | Typed API client that mirrors the backend models |
| `components/CrawlForm.tsx` | URL input and "Generate Test Cases" |
| `components/SummaryCards.tsx` | Total / Passed / Failed / Errors |
| `components/TestCaseTable.tsx` | Test table with include toggles, expandable details, logs, and failure screenshots |
| `components/AddTestModal.tsx` | "Add Manual Test Case" dialog |
| `components/DownloadMenu.tsx` | HTML / CSV / JSON report download |

Screenshots use a plain `<img>` because Next 16's `next/image` blocks local-IP image
sources by default.
