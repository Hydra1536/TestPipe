import asyncio
import logging
import sys
import threading
import uuid
from typing import Literal, Optional

# On Windows, `uvicorn --reload` installs the Selector event-loop policy, which can't
# spawn subprocesses, so Playwright fails with NotImplementedError when launching the
# browser. uvicorn's own loop already exists by the time this module is imported, so
# switching the policy here only affects the loops Playwright creates.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from . import groq_client, storage
from .config import settings
from .executor import run_tests
from .models import (
    CrawlRequest,
    ManualLoginRequest,
    ManualTestCaseCreate,
    RunTestsRequest,
    StepAction,
    TestCase,
    TestCaseUpdate,
    TestRun,
)
from .reports import to_csv, to_html, to_json, to_pdf
from .scraper import CrawlError, crawl_site, manual_login

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("vaitestpipe")
# The UI polls /api/jobs/{id} every second; keep those out of the access log.
logging.getLogger("uvicorn.access").addFilter(lambda r: "/api/jobs/" not in r.getMessage())

app = FastAPI(title="VaiTestPipe API", version="0.2.0")
# Compress large JSON payloads (test-case lists, run results) over the wire. Browsers
# send Accept-Encoding: gzip and decode transparently, so both ends exchange the
# compact, binary-compressed form instead of raw multi-KB JSON.
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)
app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")

# One browser run at a time; a second click on "Run Tests" gets a 409.
_run_lock = threading.Lock()


def _groq_http_error(e: Exception) -> HTTPException:
    if isinstance(e, groq_client.GroqNotConfigured):
        return HTTPException(503, str(e))
    return HTTPException(502, str(e))


# NOTE: endpoints that touch Playwright or Groq are plain `def` on purpose. FastAPI runs
# them in a worker thread, which is what the sync Playwright API needs.

@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


@app.get("/api/health")
def health():
    return {"status": "ok", "groq_configured": bool(settings.groq_api_key), "model": settings.groq_model}


@app.get("/api/step-actions")
def step_actions():
    return [a.value for a in StepAction]


def _crawl_summary(crawl) -> dict:
    return {
        "start_url": crawl.start_url,
        "crawled_at": crawl.crawled_at,
        "page_count": len(crawl.pages),
        "auth": crawl.auth.model_dump(mode="json"),
        "pages": [
            {
                "url": p.final_url,
                "title": p.title,
                "screenshot": p.screenshot,
                "element_count": len(p.elements),
                "requires_auth": p.requires_auth,
            }
            for p in crawl.pages
        ],
    }


@app.post("/api/crawl")
def crawl_and_generate(req: CrawlRequest):
    return _crawl_and_generate(req)


def _crawl_and_generate(req: CrawlRequest, progress=None):
    """`progress(phase, done, total)`: optional job progress callback."""
    progress = progress or (lambda *a: None)
    url = str(req.url)
    try:
        site = crawl_site(
            url,
            max_pages=req.max_pages,
            same_domain_only=req.same_domain_only,
            auth=req.auth,
            on_page=lambda n: progress("crawling", n, None),
        )
    except CrawlError as e:
        raise HTTPException(422, str(e))
    storage.save_site_crawl(site)
    log.info("Crawled %s: %d pages (auth=%s)", url, len(site.pages), site.auth.mode)
    progress("generating", 0, len(site.pages))

    try:
        generated = groq_client.generate_for_site(
            site.pages, req.max_tests_per_page,
            lambda done, total, *_: progress("generating", done, total),
        )
    except (groq_client.GroqNotConfigured, groq_client.GroqGenerationError) as e:
        raise _groq_http_error(e)

    saved = (storage.replace_ai_test_cases if req.replace_existing_ai else storage.append_test_cases)(generated)
    return {
        "crawl": _crawl_summary(site),
        "generated_count": len(saved),
        "test_cases": storage.list_test_cases(),
    }


# Background jobs (crawl, test run, manual login), so the browser polls progress instead
# of holding one HTTP request open for minutes.
# ponytail: in-memory, lost on restart and never pruned; fine for a single local server.
_jobs: dict[str, dict] = {}
_login_stops: dict[str, threading.Event] = {}


def _start_job(fn, on_exit=None) -> str:
    """Runs fn(progress) in a thread; progress(phase, done, total) updates the job."""
    job_id = uuid.uuid4().hex[:12]
    job = _jobs[job_id] = {"id": job_id, "status": "running", "phase": None, "done": 0, "total": None}

    def progress(phase: str, done: int, total: Optional[int]) -> None:
        job.update(phase=phase, done=done, total=total)

    def work() -> None:
        try:
            job.update(status="done", result=jsonable_encoder(fn(progress)))
        except HTTPException as e:
            job.update(status="error", status_code=e.status_code, detail=e.detail)
        except Exception as e:  # noqa: BLE001 - surface anything to the poller instead of hanging it
            log.exception("Job %s failed", job_id)
            job.update(status="error", status_code=500, detail=str(e))
        finally:
            if on_exit:
                on_exit()

    threading.Thread(target=work, daemon=True).start()
    return job_id


def _job_running() -> bool:
    return any(j["status"] == "running" for j in _jobs.values())


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job


@app.post("/api/crawl/jobs", status_code=202)
def start_crawl_job(req: CrawlRequest):
    return {"job_id": _start_job(lambda progress: _crawl_and_generate(req, progress))}


@app.get("/api/crawl/latest")
def latest_crawl():
    site = storage.get_site_crawl()
    if not site:
        raise HTTPException(404, "Nothing crawled yet.")
    return _crawl_summary(site)


@app.get("/api/test-cases", response_model=list[TestCase])
def get_test_cases():
    return storage.list_test_cases()


@app.post("/api/test-cases/add", response_model=TestCase, status_code=201)
def add_manual_test_case(body: ManualTestCaseCreate):
    snapshot = storage.get_snapshot()
    target_url = str(body.target_url) if body.target_url else (snapshot.final_url if snapshot else None)
    if not target_url:
        raise HTTPException(400, "No target_url given and no page has been crawled yet.")

    steps = body.steps
    if steps is None:
        # Compile the plain-English steps. If Groq is unavailable we still save the
        # test; it will report Error on run until steps are added via PUT.
        try:
            same_page = snapshot if snapshot and snapshot.final_url == target_url else None
            steps = groq_client.compile_manual_steps(
                body.title, body.action_steps, body.expected_result, target_url, same_page
            )
        except (groq_client.GroqNotConfigured, groq_client.GroqGenerationError) as e:
            log.warning("Could not compile manual steps: %s", e)
            steps = []

    tc = TestCase(
        id="pending",
        title=body.title,
        description=body.description,
        action_steps=body.action_steps,
        expected_outcome=body.expected_result,
        test_type=body.test_type,
        source="manual",
        target_url=target_url,
        steps=steps,
    )
    return storage.append_test_cases([tc])[0]


@app.put("/api/test-cases/{test_id}", response_model=TestCase)
def edit_test_case(test_id: str, body: TestCaseUpdate):
    updated = storage.update_test_case(test_id, body.model_dump(exclude_unset=True))
    if not updated:
        raise HTTPException(404, f"Test case {test_id} not found")
    return updated


@app.delete("/api/test-cases/{test_id}", status_code=204)
def remove_test_case(test_id: str):
    if not storage.delete_test_case(test_id):
        raise HTTPException(404, f"Test case {test_id} not found")


def _cases_to_run(body: Optional[RunTestsRequest]) -> list[TestCase]:
    cases = [c for c in storage.list_test_cases() if c.enabled]
    if body and body.test_ids is not None:
        wanted = set(body.test_ids)
        cases = [c for c in cases if c.id in wanted]
    if not cases:
        raise HTTPException(400, "No test cases to run.")
    return cases


def _run_and_save(cases: list[TestCase], on_progress=None) -> TestRun:
    run = run_tests(cases, on_progress)
    storage.save_run(run)
    log.info("Run %s: %d/%d passed", run.run_id, run.passed, run.total)
    return run


@app.post("/api/run-tests", response_model=TestRun)
def run_all_tests(body: Optional[RunTestsRequest] = None):
    cases = _cases_to_run(body)
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "A test run is already in progress.")
    try:
        return _run_and_save(cases)
    finally:
        _run_lock.release()


@app.post("/api/run-tests/jobs", status_code=202)
def start_run_job(body: Optional[RunTestsRequest] = None):
    cases = _cases_to_run(body)
    if not _run_lock.acquire(blocking=False):
        raise HTTPException(409, "A test run is already in progress.")
    job_id = _start_job(
        lambda progress: _run_and_save(cases, lambda done, total: progress("running", done, total)),
        on_exit=_run_lock.release,
    )
    return {"job_id": job_id}


# ---- Manual login: a real browser window where the user logs in themselves -----

@app.post("/api/auth/manual-login", status_code=202)
def start_manual_login(body: ManualLoginRequest):
    stop = threading.Event()

    def work(progress):
        progress("waiting_for_login", 0, None)
        try:
            return manual_login(str(body.login_url), stop)
        except CrawlError as e:
            raise HTTPException(422, str(e))

    job_id = _start_job(work)
    _login_stops[job_id] = stop
    return {"job_id": job_id}


@app.post("/api/auth/manual-login/{job_id}/finish")
def finish_manual_login(job_id: str):
    stop = _login_stops.get(job_id)
    if not stop:
        raise HTTPException(404, f"Login job {job_id} not found")
    stop.set()
    return {"ok": True}


@app.post("/api/reset")
def reset_everything():
    """Refresh button: wipe test cases, runs, the last crawl, the saved session and screenshots."""
    if _job_running() or _run_lock.locked():
        raise HTTPException(409, "A crawl, test run or login is still in progress.")
    storage.reset_all()
    return {"ok": True}


@app.get("/api/runs")
def list_runs():
    return [
        {k: v for k, v in r.model_dump(mode="json").items() if k != "results"}
        for r in storage.list_runs()
    ]


@app.get("/api/runs/latest", response_model=TestRun)
def latest_run():
    run = storage.get_run()
    if not run:
        raise HTTPException(404, "No test runs yet.")
    return run


@app.get("/api/reports/download")
def download_report(
    request: Request,
    format: Literal["json", "csv", "html", "pdf"] = Query("json"),
    run_id: Optional[str] = None,
):
    run = storage.get_run(run_id)
    if not run:
        raise HTTPException(404, "No test run found. Run tests first.")
    cases = storage.list_test_cases()
    base = str(request.base_url).rstrip("/")

    if format == "pdf":
        return Response(
            content=to_pdf(run, cases),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="report-{run.run_id}.pdf"'},
        )
    if format == "csv":
        body, media = to_csv(run, cases), "text/csv"
    elif format == "html":
        body, media = to_html(run, cases, base), "text/html"
    else:
        body, media = to_json(run, cases), "application/json"
    return Response(
        content=body,
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{run.run_id}.{format}"'},
    )
