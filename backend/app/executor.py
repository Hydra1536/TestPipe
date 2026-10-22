"""Test execution engine: interprets TestStep lists with Playwright.

Status semantics:
  Pass  - every step ran and every expect_* assertion held
  Fail  - an expect_* assertion did not hold (the site behaved differently)
  Error - the test could not run (no steps, element not found for an action,
          navigation failure, bad selector, ...)
"""
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright

from . import asset_cache
from .config import settings
from .models import ASSERTION_ACTIONS, StepAction, TestCase, TestResult, TestRun, TestStatus, TestStep, utcnow
from .scraper import wait_for_render

MAX_WAIT_MS = 5000


def _describe(step: TestStep) -> str:
    parts = [step.action.value]
    if step.selector:
        parts.append(f"selector={step.selector!r}")
    if step.value is not None:
        parts.append(f"value={step.value!r}")
    return " ".join(parts)


def _require(step: TestStep, field: str) -> str:
    val = getattr(step, field)
    if not val:
        raise ValueError(f"'{step.action.value}' requires a {field}")
    return val


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}  # never let a generated test change data


def _last_response(page: Page, state: dict):
    """Response of the last `request` step, else a fresh GET of the current page."""
    if state.get("response") is None:
        state["response"] = page.request.get(page.url, fail_on_status_code=False)
    return state["response"]


def _check(ok: bool, msg: str) -> None:
    if not ok:
        raise AssertionError(msg)


def _run_step(page: Page, step: TestStep, base_url: str, state: dict) -> None:
    a = step.action
    loc = lambda: page.locator(_require(step, "selector")).first  # noqa: E731

    if a == StepAction.request:
        method, _, target = _require(step, "value").strip().partition(" ")
        method = method.upper()
        if method not in SAFE_METHODS:
            raise ValueError(f"request method {method} not allowed; use GET, HEAD or OPTIONS")
        headers = json.loads(step.selector) if step.selector else None
        for k, v in (headers or {}).items():  # "Bearer {{localStorage.token}}" -> real token
            headers[k] = re.sub(r"\{\{localStorage\.([^}]+)\}\}",
                                lambda m: page.evaluate("k => localStorage.getItem(k) || ''", m.group(1)), str(v))
        state["response"] = page.request.fetch(
            urljoin(page.url or base_url, target.strip() or page.url), method=method,
            headers=headers, fail_on_status_code=False, max_redirects=0,
        )
        return
    if a == StepAction.expect_status:
        want, got = _require(step, "value").strip().lower(), _last_response(page, state).status
        ok = str(got)[0] == want[0] if want.endswith("xx") else str(got) == want
        return _check(ok, f"expected HTTP status {want}, got {got}")
    if a == StepAction.expect_header:
        name = _require(step, "selector").lower()
        got = _last_response(page, state).headers.get(name)
        _check(got is not None, f"response header {name!r} is missing")
        return _check(not step.value or step.value.lower() in got.lower(),
                      f"header {name!r} is {got!r}, expected it to contain {step.value!r}")
    if a == StepAction.expect_header_absent:
        name = _require(step, "selector").lower()
        got = _last_response(page, state).headers.get(name)
        return _check(got is None, f"response header {name!r} should be absent but is {got!r}")
    if a == StepAction.expect_no_dialog:
        return _check(not state["dialogs"], f"page opened a dialog: {state['dialogs'][:3]}")
    if a == StepAction.expect_url_not:
        bad = _require(step, "value")
        return _check(bad.lower() not in page.url.lower(), f"URL {page.url!r} contains {bad!r}")
    if a == StepAction.expect_not_text:
        target = page.locator(step.selector).first if step.selector else page.locator("body")
        expect(target).not_to_contain_text(_require(step, "value"), ignore_case=True)
        return

    if a == StepAction.goto:
        page.goto(urljoin(base_url, _require(step, "value")), wait_until="domcontentloaded",
                  timeout=settings.nav_timeout_ms)
    elif a == StepAction.go_back:
        page.go_back(wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
    elif a == StepAction.reload:
        page.reload(wait_until="domcontentloaded", timeout=settings.nav_timeout_ms)
    elif a == StepAction.click:
        loc().click()
    elif a == StepAction.fill:
        loc().fill(step.value or "")
    elif a == StepAction.press:
        key = _require(step, "value")
        loc().press(key) if step.selector else page.keyboard.press(key)
    elif a == StepAction.select:
        loc().select_option(_require(step, "value"))
    elif a == StepAction.check:
        loc().check()
    elif a == StepAction.hover:
        loc().hover()
    elif a == StepAction.wait:
        page.wait_for_timeout(min(int(step.value or 1000), MAX_WAIT_MS))
    elif a == StepAction.expect_visible:
        expect(loc()).to_be_visible()
    elif a == StepAction.expect_hidden:
        expect(loc()).to_be_hidden()
    elif a == StepAction.expect_text:
        target = page.locator(step.selector).first if step.selector else page.locator("body")
        expect(target).to_contain_text(_require(step, "value"), ignore_case=True)
    elif a == StepAction.expect_value:
        expect(loc()).to_have_value(_require(step, "value"))
    elif a == StepAction.expect_title:
        expect(page).to_have_title(re.compile(re.escape(_require(step, "value")), re.I))
    elif a == StepAction.expect_url:
        expect(page).to_have_url(re.compile(re.escape(_require(step, "value"))))
    else:
        raise ValueError(f"Unsupported action {a}")


def _screenshot(page: Page, test_id: str) -> str | None:
    name = f"{test_id}-{uuid.uuid4().hex[:8]}.png"
    try:
        page.screenshot(path=str(settings.screenshots_dir / name), timeout=5000)
        return f"/static/screenshots/{name}"
    except PlaywrightError:
        return None


def _run_test(page: Page, tc: TestCase) -> TestResult:
    logs: list[str] = []
    started = time.perf_counter()
    status, error, shot = TestStatus.passed, None, None

    def result() -> TestResult:
        return TestResult(
            test_id=tc.id, title=tc.title, test_type=tc.test_type, source=tc.source,
            page_url=tc.page_url or tc.target_url, page_title=tc.page_title,
            status=status, duration_ms=int((time.perf_counter() - started) * 1000),
            logs=logs, error=error, screenshot=shot,
        )

    if not tc.steps:
        status, error = TestStatus.error, "Test case has no executable steps."
        logs.append(error)
        return result()
    if not any(s.action in ASSERTION_ACTIONS for s in tc.steps):
        logs.append("WARNING: no assertions — test only checks that steps run without errors.")

    steps = list(tc.steps)
    if steps[0].action != StepAction.goto:
        steps.insert(0, TestStep(action=StepAction.goto, value=tc.target_url))

    # Dialogs are recorded for expect_no_dialog; Playwright auto-dismisses them.
    state: dict = {"response": None, "dialogs": []}
    page.on("dialog", lambda d: state["dialogs"].append(f"{d.type}: {d.message[:80]}"))

    for i, step in enumerate(steps, 1):
        label = f"[{i}/{len(steps)}] {_describe(step)}"
        try:
            if step.action in (StepAction.goto, StepAction.reload, StepAction.go_back):
                state["response"] = None  # header/status checks now target the new page
            _run_step(page, step, tc.target_url, state)
            # The first step opens the page under test; let the SPA render before asserting.
            # Later gotos (e.g. security checks on /.env) have no UI to wait for.
            if i == 1 and step.action == StepAction.goto and not wait_for_render(page, settings.render_timeout_ms):
                logs.append(f"{label} ... WARNING: page rendered nothing interactive in time")
            logs.append(f"{label} ... ok")
        except AssertionError as e:
            status, error = TestStatus.failed, _first_line(str(e))
        except (PlaywrightError, ValueError) as e:  # json.JSONDecodeError is a ValueError
            status, error = TestStatus.error, _first_line(getattr(e, "message", None) or str(e))
        if status != TestStatus.passed:
            logs.append(f"{label} ... {status.value.upper()}: {error}")
            shot = _screenshot(page, tc.id)
            break

    return result()


def _first_line(msg: str) -> str:
    lines = [ln.strip() for ln in msg.strip().splitlines() if ln.strip()]
    return " | ".join(lines[:3])[:500] if lines else "Unknown error"


def _block_slow_resources(route) -> None:
    if route.request.resource_type in settings.block_resources:
        route.abort()
    else:
        route.continue_()


def _run_chunk(cases: list[TestCase], auth_state, on_result) -> list[TestResult]:
    """One worker: its own Playwright + browser, since the sync API isn't thread-safe."""
    results: list[TestResult] = []
    headful = settings.run_headful
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False if headful else settings.headless,
            slow_mo=settings.run_slow_mo_ms if headful else 0,  # visible, watchable actions
        )
        try:
            for tc in cases:
                # fresh context per test: isolated cookies/storage
                ctx_kwargs = {"viewport": {"width": 1366, "height": 900}, "user_agent": settings.user_agent}
                if auth_state and tc.requires_auth:
                    ctx_kwargs["storage_state"] = str(auth_state)
                context = browser.new_context(**ctx_kwargs)
                context.set_default_timeout(settings.step_timeout_ms)
                context.set_default_navigation_timeout(settings.nav_timeout_ms)
                # Don't block resources when the run is visible: the user should see the real page.
                if settings.block_resources and not headful:
                    context.route("**/*", _block_slow_resources)
                # Registered last so it runs first: hashed JS/CSS come from disk, the rest falls through.
                asset_cache.install(context)
                try:
                    results.append(_run_test(context.new_page(), tc))
                    on_result()
                finally:
                    context.close()
        finally:
            browser.close()
    return results


def run_tests(cases: list[TestCase], on_progress=None) -> TestRun:
    """`on_progress(done, total)` is called after each finished test."""
    started_at = utcnow()
    expect.set_options(timeout=settings.step_timeout_ms)

    # Session saved during a crawl with login/sign-up. Reused only for tests whose page
    # was reached behind that login, so logged-out checks still run in a clean context.
    auth_state = settings.auth_state_file if settings.auth_state_file.exists() else None

    done, lock = 0, threading.Lock()

    def on_result() -> None:
        nonlocal done
        with lock:
            done += 1
            if on_progress:
                on_progress(done, len(cases))

    # A visible run uses a single window so the user watches one tab at a time.
    workers = 1 if settings.run_headful else max(1, min(settings.run_workers, len(cases)))
    chunks = [cases[i::workers] for i in range(workers)]
    with ThreadPoolExecutor(workers) as pool:
        by_id = {r.test_id: r for chunk in pool.map(lambda c: _run_chunk(c, auth_state, on_result), chunks) for r in chunk}
    results = [by_id[tc.id] for tc in cases]  # keep the requested order

    return TestRun(
        run_id=f"run-{uuid.uuid4().hex[:10]}",
        started_at=started_at,
        finished_at=utcnow(),
        total=len(results),
        passed=sum(r.status == TestStatus.passed for r in results),
        failed=sum(r.status == TestStatus.failed for r in results),
        errors=sum(r.status == TestStatus.error for r in results),
        results=results,
    )
