"""Groq (Groq-hosted LLM) integration: generates test cases and compiles manual steps.

The model only ever produces data in a fixed step vocabulary (see StepAction). The
executor interprets that data; nothing the model returns is executed as code.
"""
import hashlib
import json
import logging
import time
from collections import Counter
from typing import Any, Optional

from groq import BadRequestError, Groq, GroqError, NotFoundError, RateLimitError
from pydantic import ValidationError

from . import storage
from .config import settings
from .models import TEST_TYPES, PageSnapshot, StepAction, TestCase, TestStep

log = logging.getLogger(__name__)


class GroqNotConfigured(Exception):
    pass


class GroqGenerationError(Exception):
    pass


class GroqQuotaExhausted(GroqGenerationError):
    """Every configured model hit its daily token/request limit."""


# model -> time.time() until which it's skipped after hitting a daily limit.
# ponytail: fixed 10-min cooldown, parse Groq's "try again in Xm" if it matters.
_exhausted: dict[str, float] = {}
QUOTA_COOLDOWN_S = 600

# Strict JSON schemas: gpt-oss models decode against these, so output is always valid JSON.
_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [a.value for a in StepAction]},
        "selector": {"type": ["string", "null"]},
        "value": {"type": ["string", "null"]},
    },
    "required": ["action", "selector", "value"],
    "additionalProperties": False,
}
STEPS_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "items": _STEP_SCHEMA}},
    "required": ["steps"],
    "additionalProperties": False,
}
TEST_CASES_SCHEMA = {
    "type": "object",
    "properties": {"test_cases": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "action_steps": {"type": "array", "items": {"type": "string"}},
            "expected_outcome": {"type": "string"},
            "test_type": {"type": "string", "enum": [t for t in TEST_TYPES if t != "Manual"]},
            "steps": {"type": "array", "items": _STEP_SCHEMA},
        },
        "required": ["title", "description", "action_steps", "expected_outcome", "test_type", "steps"],
        "additionalProperties": False,
    }}},
    "required": ["test_cases"],
    "additionalProperties": False,
}


STEP_SPEC = """
Each executable step is an object {"action": ..., "selector": ..., "value": ...}.
Allowed actions (use ONLY these):
- goto            value = URL (absolute, or path relative to the target URL)
- go_back         (no fields) browser Back button
- reload          (no fields) reload the current page
- click           selector
- fill            selector, value = text to type
- press           selector (optional), value = key name e.g. "Enter"
- select          selector, value = option label or value
- check           selector
- hover           selector
- wait            value = milliseconds (max 5000); use sparingly
- expect_visible  selector
- expect_hidden   selector
- expect_text     selector (optional, defaults to whole page), value = text that must appear
- expect_value    selector, value = expected input value
- expect_title    value = substring expected in the page title
- expect_url      value = substring expected in the current URL
- expect_url_not  value = substring that must NOT be in the URL (tokens, passwords, emails)
- expect_not_text selector (optional), value = text that must NOT appear (stack traces,
                  database error strings, internal file paths, other users' data)
- expect_no_dialog (no fields) no alert/confirm/prompt fired after a step
- request         value = "METHOD url" (GET, HEAD or OPTIONS only; url absolute or relative);
                  selector = optional JSON object of request headers, e.g.
                  {"Origin": "https://other.example"} or
                  {"Authorization": "Bearer {{localStorage.KEY}}"} where KEY is one of the
                  page's storage_keys (the runner substitutes the real value at run time)
- expect_status   value = "200", "401", "403", "404" or a class like "4xx"; checks the last
                  `request` response, else the current page's HTTP response
- expect_header   selector = header name, value = optional substring it must contain
- expect_header_absent selector = header name that must NOT be present in the response
Rules:
- Selectors MUST come from the provided element list whenever possible. Playwright
  selector syntax is allowed (CSS, :has-text("..."), text=...).
- Never simulate navigation with keyboard shortcuts; use go_back / reload / goto.
- Every test must contain at least one expect_* step that verifies the expected outcome.
- Do not include a goto to the target URL as the first step; the runner opens it automatically.
- Only assert on things observable in the page data given. Do not invent text or elements.
- For request/expect_status/expect_header checks, use the endpoints listed in api_calls;
  do not guess random endpoints except well-known exposure paths (/.env, /.git/config,
  /swagger, /api-docs, /openapi.json, /graphql, /actuator, /server-status).
""".strip()


def _client() -> Groq:
    if not settings.groq_api_key:
        raise GroqNotConfigured("GROQ_API_KEY is not set. Add it to backend/.env.")
    return Groq(api_key=settings.groq_api_key)


def _snapshot_context(
    snapshot: PageSnapshot, max_elements: int = 60, exclude: frozenset[str] = frozenset()
) -> str:
    """`exclude`: selectors of site-wide header/footer elements already covered elsewhere."""
    elements = [
        {k: v for k, v in {
            "kind": e.kind,
            "selector": e.selector,
            "text": e.text or None,
            "attrs": e.attrs or None,
            "visible": e.visible,
        }.items() if v is not None}
        for e in [e for e in snapshot.elements if e.selector not in exclude][:max_elements]
    ]
    return json.dumps(
        {
            "url": snapshot.final_url,
            "title": snapshot.title,
            "headings": snapshot.headings,
            "elements": elements,
            "api_calls": snapshot.api_calls,      # real endpoints for auth/exposure tests
            "storage_keys": snapshot.storage_keys,  # localStorage key names (no values)
            "requires_auth": snapshot.requires_auth,
        },
        ensure_ascii=False,
    )


def _chat_json(system: str, user: str, schema_name: str, schema: dict) -> dict[str, Any]:
    client = _client()
    last_err: Optional[Exception] = None
    for model in settings.groq_models:
        if _exhausted.get(model, 0) > time.time():
            continue
        # Groq supports strict schema decoding only on gpt-oss; others get plain JSON mode.
        fmt = ({"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
               if model.startswith("openai/gpt-oss") else {"type": "json_object"})
        for _ in range(2):  # one retry on malformed JSON
            try:
                resp = client.chat.completions.create(
                    model=model,
                    temperature=0.2,
                    max_tokens=8000,  # uncapped, security-heavy suites are long
                    response_format=fmt,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                )
                return json.loads(resp.choices[0].message.content or "{}")
            except json.JSONDecodeError as e:
                last_err = e
            except RateLimitError as e:
                # Per-minute limits are already retried by the SDK; a daily cap won't clear
                # soon, so move on to the next model instead of hammering this one.
                if "per day" not in str(e):
                    raise GroqGenerationError(f"Groq API error: {e}") from e
                log.warning("Groq model %s hit its daily limit; trying the next model.", model)
                _exhausted[model] = time.time() + QUOTA_COOLDOWN_S
                last_err = e
                break
            except NotFoundError as e:
                # Model removed or not enabled for this key: skip it for good.
                log.warning("Groq model %s is not available (%s); trying the next model.", model, e)
                _exhausted[model] = float("inf")
                last_err = e
                break
            except BadRequestError as e:
                # JSON mode reports unparseable model output as 400 json_validate_failed
                if "json_validate_failed" not in str(e):
                    raise GroqGenerationError(f"Groq API error: {e}") from e
                last_err = e
            except GroqError as e:
                raise GroqGenerationError(f"Groq API error: {e}") from e
    if last_err is None or isinstance(last_err, (RateLimitError, NotFoundError)):
        unavailable = [m for m in settings.groq_models if _exhausted.get(m) == float("inf")]
        limited = [m for m in settings.groq_models if m not in unavailable]
        raise GroqQuotaExhausted(
            "No Groq model is usable right now. Daily limit reached: " + (", ".join(limited) or "none")
            + ". Not available for this API key: " + (", ".join(unavailable) or "none")
            + ". Try again later or change GROQ_MODELS."
        )
    raise GroqGenerationError(f"Groq returned invalid JSON: {last_err}")


def _parse_steps(raw: Any) -> list[TestStep]:
    """Keep valid steps, drop anything outside the vocabulary."""
    steps: list[TestStep] = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        try:
            step = TestStep(**{k: (str(v) if v is not None else None) for k, v in s.items()
                               if k in ("action", "selector", "value")})
        except ValidationError:
            log.warning("Dropping invalid step from model: %s", s)
            continue
        steps.append(step)
    return steps


COVERAGE_GUIDANCE = """
Guiding principle: "Never trust user input." Be exhaustive and creative. Generate every
distinct, meaningful test this page warrants — there is NO fixed limit, so favour breadth
and edge cases. Order the output by value: SECURITY and USABILITY first and in the
greatest number, then correctness/robustness, and only a few low-value UI-styling tests.

ALWAYS include at least one test of EACH of these types for the page (in addition to the
security/usability focus below):
  - Smoke: the page loads and a key heading/element is present.
  - UI (UI / Visual): important visible elements render and are labelled (keep these few).
  - Regression: a core end-to-end interaction on this page still works (a known-good flow).
  - Navigation: each nav control / link leads to the right place.
  - Performance: after reload, a key element becomes visible (page renders without hanging).
  - Accessibility: inputs have labels/aria and interactive elements are reachable.

SECURITY (put the most effort here; these are defensive, non-destructive checks on a site
you are authorised to test). Cover each item this page makes testable:
  - Injection resilience: submit malformed / injection-style strings (SQL/NoSQL syntax,
    HTML/script markup, template markers) into each input; assert the app treats them as
    literal data — the raw text is escaped on screen (expect_not_text on any database or
    parser error string), no script dialog fires (expect_no_dialog), and the page does not
    break.
  - Broken object-level authorisation / IDOR: for an api_calls endpoint that includes an
    id, `request` a different or adjacent id (GET only) and expect_status 401/403/404, not 200.
  - Broken authentication: `request` a protected api_calls endpoint with the Authorization
    header omitted, and separately with a clearly invalid token; expect_status 401/403.
  - Excessive data exposure: `request` a data endpoint and expect_not_text for fields that
    should never reach the client (password, password_hash, secret, ssn, card number).
  - Sensitive data in URL: after login/search/submit, expect_url_not for password, token,
    email, session id.
  - Security headers on the page response: expect_header for content-security-policy,
    x-content-type-options=nosniff, x-frame-options or a frame-ancestors CSP (clickjacking),
    strict-transport-security; expect_header_absent for server and x-powered-by
    (version disclosure).
  - CORS policy: `request` an api_calls endpoint with a foreign Origin header and
    expect the response NOT to reflect it as access-control-allow-origin (expect_header_absent
    or expect_header not matching the foreign origin).
  - Improper / shadow assets: `request` the well-known exposure paths and expect_status 404
    (never 200): /.env, /.git/config, /swagger, /api-docs, /openapi.json, /actuator.
  - Sensitive error messages / logs leakage: force a 404/400 and expect_not_text for stack
    traces, framework names, file-system paths.
  - Session handling: after logout, `request` a protected endpoint and expect_status 401/403
    (improper session termination).
  - Rate limiting / anti-automation: note in the description where brute-force protection
    should exist; do NOT actually flood requests.
  Do NOT perform destructive, brute-force or high-volume attacks, and use only GET/HEAD/
  OPTIONS with `request`.

USABILITY (second priority): clear labels and placeholders; helpful, specific validation
messages on bad input; required fields flagged; primary action reachable and obvious;
keyboard/Enter submits a form; error and empty states are understandable; back/reload keep
the user where they expect.

ACCESSIBILITY: inputs have labels/aria; interactive elements are reachable and named.

CORRECTNESS / NAVIGATION / FORMS: smoke (page + key heading load), each internal link/button
leads somewhere sensible, form validation for required/invalid/boundary values.

SCALABILITY / ROBUSTNESS: very long input is accepted or gracefully rejected; lists/tables
paginate or lazy-load; the page still works after a reload.

UI styling/layout tests: keep these FEW — only genuinely important visible elements.

Every test must be deterministic and non-destructive (never submit real payments, delete
data, or send real messages). Skip exact duplicates and trivial variations.
""".strip()


def generate_test_cases(
    snapshot: PageSnapshot, max_tests: Optional[int] = None, exclude: frozenset[str] = frozenset()
) -> list[TestCase]:
    types = ", ".join(f'"{t}"' for t in TEST_TYPES if t != "Manual")
    system = (
        "You are a senior QA automation engineer and application security tester. You "
        "write concise, high-value frontend test cases and express each as executable "
        "Playwright steps.\n\n"
        + STEP_SPEC
        + "\n\n"
        + COVERAGE_GUIDANCE
        + "\n\nRespond with a JSON object: {\"test_cases\": [ {"
        "\"title\": str, \"description\": str, \"action_steps\": [str], "
        "\"expected_outcome\": str, "
        f"\"test_type\": one of [{types}], "
        "\"steps\": [step] } ]}"
    )
    shared_note = (
        "Site-wide header/footer/nav elements were removed from this page's data because "
        "they are tested on another page; focus on this page's own content.\n\n"
        if exclude else ""
    )
    how_many = f"up to {max_tests}" if max_tests else "as many high-value"
    user = (
        f"Generate {how_many} test cases for this single page, security and usability first. "
        f"The site may be behind a login you already have access to.\n\n"
        f"{shared_note}PAGE DATA:\n{_snapshot_context(snapshot, exclude=exclude)}"
    )
    # Unchanged page + same prompt = same tests; skip the Groq call entirely.
    cache_key = hashlib.sha1(f"{system}\n{user}\n{snapshot.requires_auth}".encode()).hexdigest()
    cached = storage.get_cached_generation(cache_key)
    if cached:
        log.info("Using cached tests for %s", snapshot.final_url)
        return [TestCase(**c) for c in cached]
    data = _chat_json(system, user, "test_cases", TEST_CASES_SCHEMA)

    raw_cases = data.get("test_cases") or []
    cases: list[TestCase] = []
    for raw in (raw_cases[:max_tests] if max_tests else raw_cases):
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        steps = _parse_steps(raw.get("steps"))
        action_steps = raw.get("action_steps") or []
        cases.append(
            TestCase(
                id="pending",
                title=str(raw["title"]),
                description=str(raw.get("description", "")),
                action_steps=[str(s) for s in action_steps] if isinstance(action_steps, list) else [str(action_steps)],
                expected_outcome=str(raw.get("expected_outcome", "")),
                test_type=str(raw.get("test_type", "Functional")),
                source="ai",
                target_url=snapshot.final_url,
                page_url=snapshot.final_url,
                page_title=snapshot.title,
                requires_auth=snapshot.requires_auth,
                steps=steps,
            )
        )
    if not cases:
        raise GroqGenerationError("Model returned no usable test cases.")
    storage.put_cached_generation(cache_key, [c.model_dump(mode="json") for c in cases])
    return cases


def generate_for_site(pages, max_tests_per_page: Optional[int] = None, on_progress=None) -> list[TestCase]:
    """Generate test cases for every crawled page. Failures on one page are logged and
    skipped so a single bad page doesn't abort the whole batch."""
    all_cases: list[TestCase] = []
    shared = _shared_selectors(pages)
    for i, snap in enumerate(pages, 1):
        try:
            # Site-wide elements are tested once, on the first page only.
            cases = generate_test_cases(snap, max_tests_per_page, frozenset() if i == 1 else shared)
            all_cases.extend(cases)
        except GroqQuotaExhausted as e:
            log.warning("Stopping generation after %d/%d pages: %s", i - 1, len(pages), e)
            if not all_cases:
                raise
            break
        except GroqGenerationError as e:
            log.warning("No tests generated for %s: %s", snap.final_url, e)
            cases = []
        if on_progress:
            on_progress(i, len(pages), snap.final_url, len(cases))
    if not all_cases:
        raise GroqGenerationError("The model returned no usable test cases for any page.")
    return all_cases


def _shared_selectors(pages) -> frozenset[str]:
    """Selectors present on at least half the pages (header, nav, footer)."""
    if len(pages) < 3:
        return frozenset()
    counts = Counter(sel for p in pages for sel in {e.selector for e in p.elements})
    return frozenset(sel for sel, n in counts.items() if n * 2 >= len(pages))


def compile_manual_steps(
    title: str,
    action_steps: list[str],
    expected_result: str,
    target_url: str,
    snapshot: Optional[PageSnapshot],
) -> list[TestStep]:
    """Translate human-written steps into executable steps."""
    system = (
        "You convert manual QA test steps written in plain English into executable "
        "Playwright steps.\n\n" + STEP_SPEC
        + "\n\nRespond with a JSON object: {\"steps\": [step]}"
    )
    page = _snapshot_context(snapshot) if snapshot else json.dumps({"url": target_url, "elements": []})
    user = (
        f"Target URL: {target_url}\nTest title: {title}\n"
        f"Manual steps:\n" + "\n".join(f"{i + 1}. {s}" for i, s in enumerate(action_steps))
        + f"\nExpected result: {expected_result}\n\nPAGE DATA:\n{page}"
    )
    return _parse_steps(_chat_json(system, user, "steps", STEPS_SCHEMA).get("steps"))

