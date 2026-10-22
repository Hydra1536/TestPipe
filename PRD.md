# Product Requirement Document (PRD): AI-Powered Automated Testing Pipeline (MVP)

## 1. Overview
An automated testing pipeline web application where users input a target website URL. The system crawls the page, uses the Groq API (Groq-hosted LLM) to generate frontend test cases, allows manual test case addition/editing, executes tests using Playwright, and displays a visual dashboard with downloadable reports.

## 2. Tech Stack
- **Backend:** Python, FastAPI, Playwright (for scraping & automation), Groq API (Groq-hosted LLM for AI test generation).
- **Frontend:** Next.js (React), Tailwind CSS (Simple & clean MVP UI).
- **Data Storage:** JSON-based local storage or lightweight SQLite for test cases and execution logs.

---

## 3. Core Features & Requirements

### Feature A: URL Input & Web Crawling
- **Input:** User enters a website URL in the Next.js frontend.
- **Action:** FastAPI backend triggers Playwright to load the URL, extract the DOM structure, interactive elements (`<input>`, `<button>`, `<a>`, forms), and take a screenshot.

### Feature B: AI-Driven Test Case Generation
- **Action:** Backend sends the extracted DOM information to the Groq API (Groq-hosted LLM model).
- **Output:** AI generates structured test cases (Test ID, Title, Description, Expected Outcome, Test Type) returned in a JSON format and saved to `test_cases.json`.

### Feature C: Manual Test Case Addition (Human-in-the-Loop)
- **Requirement:** A dedicated section in the Next.js UI where users can view AI-generated test cases and manually add new custom test cases (Title, Action Steps, Expected Result).
- **Action:** Manually added test cases get appended to `test_cases.json` so they run alongside AI test cases.

### Feature D: Automated Test Execution
- **Action:** When the user clicks "Run Tests", Playwright executes all test cases (AI-generated + Manually added) against the target website.
- **Result Tracking:** Records status (`Pass`, `Fail`, `Error`) and execution logs for each test case.

### Feature E: Visual Dashboard & Reporting
- **Dashboard UI:** Displays a clean visual summary: Total Tests, Passed, Failed, and a detailed table of test cases with their statuses.
- **Downloadable Reports:** Users can download the test cases and execution reports (as JSON or CSV/HTML).

---

## 4. API Endpoints Specification (FastAPI)
- `POST /api/crawl`: Accepts URL, crawls DOM, sends to Groq, saves and returns test cases.
- `GET /api/test-cases`: Retrieves current list of test cases (AI + Manual).
- `POST /api/test-cases/add`: Adds a manual test case.
- `POST /api/run-tests`: Executes Playwright tests for all active test cases and returns results.
- `GET /api/reports/download`: Downloads the final test report as a JSON/CSV file.