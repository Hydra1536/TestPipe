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


# COVERAGE_GUIDANCE = """
# Guiding principle: "Never trust user input." Be exhaustive and creative. Generate every
# distinct, meaningful test this page warrants — there is NO fixed limit, so favour breadth
# and edge cases. Order the output by value: SECURITY and USABILITY first and in the
# greatest number, then correctness/robustness, and only a few low-value UI-styling tests.

# ALWAYS include at least one test of EACH of these types for the page (in addition to the
# security/usability focus below):
#   - Smoke: the page loads and a key heading/element is present.
#   - UI (UI / Visual): important visible elements render and are labelled (keep these few).
#   - Regression: a core end-to-end interaction on this page still works (a known-good flow).
#   - Navigation: each nav control / link leads to the right place.
#   - Performance: after reload, a key element becomes visible (page renders without hanging).
#   - Accessibility: inputs have labels/aria and interactive elements are reachable.

# SECURITY (put the most effort here; these are defensive, non-destructive checks on a site
# you are authorised to test). Cover each item this page makes testable:
#   - Injection resilience: submit malformed / injection-style strings (SQL/NoSQL syntax,
#     HTML/script markup, template markers) into each input; assert the app treats them as
#     literal data — the raw text is escaped on screen (expect_not_text on any database or
#     parser error string), no script dialog fires (expect_no_dialog), and the page does not
#     break.
#   - Broken object-level authorisation / IDOR: for an api_calls endpoint that includes an
#     id, `request` a different or adjacent id (GET only) and expect_status 401/403/404, not 200.
#   - Broken authentication: `request` a protected api_calls endpoint with the Authorization
#     header omitted, and separately with a clearly invalid token; expect_status 401/403.
#   - Excessive data exposure: `request` a data endpoint and expect_not_text for fields that
#     should never reach the client (password, password_hash, secret, ssn, card number).
#   - Sensitive data in URL: after login/search/submit, expect_url_not for password, token,
#     email, session id.
#   - Security headers on the page response: expect_header for content-security-policy,
#     x-content-type-options=nosniff, x-frame-options or a frame-ancestors CSP (clickjacking),
#     strict-transport-security; expect_header_absent for server and x-powered-by
#     (version disclosure).
#   - CORS policy: `request` an api_calls endpoint with a foreign Origin header and
#     expect the response NOT to reflect it as access-control-allow-origin (expect_header_absent
#     or expect_header not matching the foreign origin).
#   - Improper / shadow assets: `request` the well-known exposure paths and expect_status 404
#     (never 200): /.env, /.git/config, /swagger, /api-docs, /openapi.json, /actuator.
#   - Sensitive error messages / logs leakage: force a 404/400 and expect_not_text for stack
#     traces, framework names, file-system paths.
#   - Session handling: after logout, `request` a protected endpoint and expect_status 401/403
#     (improper session termination).
#   - Rate limiting / anti-automation: note in the description where brute-force protection
#     should exist; do NOT actually flood requests.
#   Do NOT perform destructive, brute-force or high-volume attacks, and use only GET/HEAD/
#   OPTIONS with `request`.

# USABILITY (second priority): clear labels and placeholders; helpful, specific validation
# messages on bad input; required fields flagged; primary action reachable and obvious;
# keyboard/Enter submits a form; error and empty states are understandable; back/reload keep
# the user where they expect.

# ACCESSIBILITY: inputs have labels/aria; interactive elements are reachable and named.

# CORRECTNESS / NAVIGATION / FORMS: smoke (page + key heading load), each internal link/button
# leads somewhere sensible, form validation for required/invalid/boundary values.

# SCALABILITY / ROBUSTNESS: very long input is accepted or gracefully rejected; lists/tables
# paginate or lazy-load; the page still works after a reload.

# UI styling/layout tests: keep these FEW — only genuinely important visible elements.

# Every test must be deterministic and non-destructive (never submit real payments, delete
# data, or send real messages). Skip exact duplicates and trivial variations.
# """.strip()

COVERAGE_GUIDANCE = """
Playwright Test Case Generation — Exhaustive Security, Usability & Functional Coverage

Core Objective:
Act as a senior QA automation engineer, application security tester, and Playwright test architect. Analyze the provided page, its UI components, user interactions, visible data, navigation, and available API calls. Generate an exhaustive collection of meaningful, distinct, deterministic, and non-destructive Playwright test cases for this specific page. Guiding principle: "Never trust user input, user-provided IDs, client-side validation, or browser state." Be exhaustive and creative. There is no fixed limit on the number of test cases. Prioritize breadth, meaningful edge cases, and realistic failure scenarios over an arbitrary test count.

Order test cases by importance:
1. Security — highest priority and greatest coverage.
2. Usability and accessibility.
3. Correctness and business logic.
4. Robustness and error handling.
5. Performance-related browser behaviour where measurable.
6. A small number of visual and styling checks.
Do not generate generic tests unrelated to the actual page. Every test must be justified by a visible component, user interaction, data field, navigation path, or API endpoint associated with the page.
All generated tests MUST be implementable using Playwright.

Use:
- Playwright browser interactions (page, locator, getByRole, getByLabel, etc.).
- Playwright API testing (request / APIRequestContext).
- Browser context cookies, local storage, session storage, and page state.
- Network interception and response inspection (page.route, page.on('response')).
- Browser console and page error monitoring.
- Dialog handling.
- Navigation and URL assertions.
- File upload and download testing when the page supports them.
- Do not generate tests that require direct database access, shell commands, Docker access, SSH, cloud-console access, CI/CD access, or infrastructure administration.
- Do not claim to verify internal encryption at rest, database privileges, container capabilities, firewall rules, IAM policies, SBOM signatures, runtime system calls, or infrastructure configurations through browser tests.
- Do not generate tests for security controls that cannot be observed through the page, browser, or accessible HTTP responses.
- For infrastructure-related controls, generate a Playwright test only when a meaningful externally observable behaviour exists.
- If a security control cannot be verified through Playwright, omit it rather than creating a fake or misleading test.
- Never use destructive actions, real payments, actual account deletion, real message delivery, or production data modification.
- Use disposable test accounts, seeded test records, controlled fixtures, and safe test environments.
- Direct API requests used for security probing must use GET, HEAD, or OPTIONS only. Do not send destructive or state-changing API requests.
- UI-based interactions may use safe, reversible test data where the test environment supports it. Never trigger irreversible actions.
Every generated test case MUST identify which team is primarily responsible for fixing the issue if the test fails.

Use exactly one of these ownership prefixes for each test case:
- Devops: <test case>
- Shared: <test case>
- Dev: <test case>
Do not use alternative labels such as "Owner", "Responsible Team", or "Category" instead of these prefixes.
Dev: Developer / Codebase

Use Dev: when the problem primarily belongs to application code, frontend behaviour, backend APIs, authentication, authorization, business logic, input validation, data handling, or application-level security.

Examples:
- BOLA / IDOR.
- RBAC and resource ownership.
- SQL/NoSQL injection handling.
- Mass assignment.
- JWT validation.
- Session revocation logic.
- Sensitive API data exposure.
- Business logic bypass.
- File upload validation.
- User enumeration.
- Race-condition protection.
- Application-level rate limiting.
- Webhook signature verification, where observable.
Devops: Infrastructure / Deployment

Use Devops: when the problem primarily belongs to server configuration, reverse proxies, TLS infrastructure, deployment configuration, infrastructure exposure, or production environment configuration.

Examples:
- Missing HTTPS enforcement.
- Missing HSTS.
- Insecure TLS configuration, when externally observable.
- Exposed .env or .git files.
- Publicly exposed internal API documentation due to deployment configuration.
- Missing or incorrectly configured reverse-proxy security headers.
- Server technology/version disclosure.
- Production debug pages exposed by deployment configuration.
- Insecure cookie transport configuration caused by deployment/proxy settings.
Shared: Developer + DevOps

Use Shared: when application code and infrastructure configuration both contribute to the security control.

Examples:
- CORS.
- Security headers.
- Rate limiting.
- SSRF protection.
- Session cookie security.
- TLS and sensitive-data communication.
- Logging and monitoring.
- API documentation exposure.
- Dependency security.
- DoS protection.
- File upload security.
- Access-control enforcement across application and infrastructure boundaries.
Choose the primary fixing responsibility based on the nature of the failure. Do not classify every security issue as Shared.
Generate every applicable security test supported by this page.
Cover:
1. Unauthenticated access to protected pages.
2. Unauthenticated access to protected API endpoints.
3. Access with a clearly invalid authentication token.
4. Expired access-token behaviour, where test fixtures support it.
5. Invalid or malformed JWT handling, where safely testable.
6. Authorization enforcement for every visible protected action.
7. Role-based access control (RBAC).
8. Users attempting to access admin-only UI features.
9. Users attempting to access admin-only API resources.
10. Broken Object-Level Authorization (BOLA).
11. Insecure Direct Object References (IDOR).
12. Changing resource IDs in URLs, route parameters, or accessible GET API requests.
13. Accessing another user's resources using adjacent or known test IDs.
14. Resource ownership enforcement.
15. Fine-grained relationship-based permissions when the page exposes relationship-dependent resources.
16. Unauthorized access to hidden or restricted UI elements through direct navigation.
17. Session expiration behaviour.
18. Session invalidation after logout.
19. Refresh-token/session reuse after logout, where safely testable.
20. Session invalidation after a password change, where the page exposes this flow and a safe fixture exists.
21. Secure cookie attributes observable through browser responses: HttpOnly, Secure, and SameSite.
22. Authentication state persistence after page refresh.
23. Authentication isolation between separate browser contexts.
24. Login and registration user enumeration through observable responses.
25. Sensitive operations requiring re-authentication or step-up authentication, when supported by the page.
26. Permission changes taking effect without relying on stale frontend state.
For IDOR tests, use GET-only requests to another known or adjacent test resource ID. Expect 401, 403, or 404 where appropriate, rather than unauthorized 200 responses.

Do not assume every 404 is a security success if the application normally uses a different documented authorization response.
For every applicable text input, search field, filter, URL field, comment field, profile field, and other user-controlled input, test relevant malformed and injection-style values.

Include:
- SQL injection-style strings.
- NoSQL operators such as $ne, $gt, and $where.
- HTML markup.
- Script tags.
- Event-handler attributes.
- JavaScript URL schemes.
- Template markers such as {{...}}, ${...}, and <%=...%>.
- Unexpected Unicode characters.
- Null bytes.
- Newline and carriage-return characters.
- Very long strings.
- Special characters and mixed encodings.
- Leading/trailing whitespace.
- Empty and whitespace-only values.
- Unexpected data types where the UI or API accepts structured input.
Assertions:
- Input is handled as literal data when appropriate.
- Rendered user-controlled content is escaped or safely sanitized.
- No unintended HTML or script execution occurs.
- No JavaScript dialog appears (expect_no_dialog).
- No database, parser, framework, or internal exception message appears (expect_not_text).
- The page remains functional.
- No unexpected navigation or broken layout occurs.
- Validation feedback is clear and appropriate.
Do not blindly inject every payload into every field. Select payloads relevant to each input's expected data type and purpose.
Where the page exposes editable data or API-backed forms, test:
1. Whether users can manipulate fields that should be read-only.
2. Whether hidden or disabled fields can be changed through browser-side manipulation.
3. Whether restricted roles can access privileged editing controls.
4. Whether changing client-side state can bypass backend restrictions.
5. Whether unexpected properties are accepted by exposed API responses or observable update flows.
6. Whether ownership, role, status, price, permissions, or other protected values can be changed through user-controlled fields.
7. Whether the application relies exclusively on frontend validation for protected operations.
Do not send state-changing API requests. Use safe UI flows and controlled test fixtures when an observable check is possible.
Test:
1. API responses do not expose password fields.
2. API responses do not expose password hashes.
3. API responses do not expose access tokens, refresh tokens, private keys, or secrets unnecessarily.
4. API responses do not expose SSNs or payment-card data.
5. Sensitive information is not accidentally rendered in the page.
6. Password and OTP values are masked where appropriate.
7. Sensitive information is not exposed in browser URLs.
8. Sensitive information is not exposed in query parameters after login, search, or submission.
9. Session identifiers and authentication tokens are not placed in URLs.
10. Sensitive values are not unnecessarily stored in local storage or session storage.
11. Sensitive values are cleared from visible UI state when the relevant flow completes, where expected.
12. Password reveal controls behave correctly and do not expose values unintentionally.
13. Externally shared links do not contain authentication or session information.
14. Sensitive data is not exposed through browser-visible error messages.
Use expect_not_text and appropriate URL, storage, DOM, and API response assertions.

Do not flag a field merely because its name contains a sensitive keyword. Evaluate whether the field is actually sensitive and whether the current user is authorized to see it.
Inspect the page's actual HTTP response headers.

Where applicable, test:
1. Content-Security-Policy.
2. X-Content-Type-Options: nosniff.
3. Clickjacking protection through X-Frame-Options or CSP frame-ancestors.
4. Strict-Transport-Security.
5. HTTPS enforcement.
6. HTTP-to-HTTPS redirection.
7. Secure cookie configuration.
8. Absence of unnecessary Server header disclosure.
9. Absence of unnecessary X-Powered-By disclosure.
10. Appropriate referrer policy.
11. Appropriate permissions policy.
Do not require both X-Frame-Options and CSP frame-ancestors if one properly configured mechanism provides the required protection.

Classify deployment-level header failures as Devops: or Shared: according to the responsibility.
Where an accessible API endpoint exists, use OPTIONS or safe GET requests with a controlled foreign Origin header.

Test:
1. A foreign origin is not incorrectly reflected in Access-Control-Allow-Origin.
2. Untrusted origins are not allowed to access credentialed resources.
3. Wildcard origins are not combined with credentials.
4. Allowed methods are appropriately restricted.
5. Allowed headers are appropriately restricted.
6. Preflight responses do not grant unnecessary cross-origin permissions.
Do not use a real malicious website. Use a controlled test origin such as https://untrusted.example.
Check applicable well-known exposure paths using GET or HEAD requests:
- /.env
- /.git/config
- /swagger
- /api-docs
- /openapi.json
- /actuator
Expect protected or nonexistent sensitive paths to return an appropriate non-success response, normally 404 or 403. Never accept a 200 response containing secrets or internal configuration.

Only test paths relevant to the actual application architecture. Do not treat a legitimate public API documentation page as a vulnerability without evidence that it exposes restricted information.
Safely trigger expected 400 and 404 responses through invalid routes, malformed query parameters, or invalid GET requests.

Verify that responses do not expose:
- Stack traces.
- Filesystem paths.
- Database errors.
- SQL statements.
- Framework internals.
- Internal server configuration.
- Secret values.
- Debug pages.
- Internal service addresses.
Check both the visible page and accessible response bodies.

Do not cause server failures or deliberately exhaust application resources.
Test only through safe, low-volume observations.
1. Identify login, registration, password-reset, OTP, search, and other sensitive endpoints.
2. Verify whether the UI communicates rate-limit feedback when a safe test fixture reaches an existing limit.
3. Verify that a 429 response is handled gracefully.
4. Verify that Retry-After information is respected where provided.
5. Verify that the UI does not expose internal rate-limit implementation details.
6. Verify that repeated safe interactions do not cause duplicate submissions.
Important: Do not flood endpoints, perform brute-force attempts, generate high-volume traffic, or attempt to bypass real account lockouts.

Mention where brute-force protection, progressive delays, temporary lockouts, and anti-automation controls should exist. If the protection cannot be safely verified with Playwright, do not fabricate a passing or failing test.
Where applicable to the page, test:
1. Protected business rules cannot be bypassed by manipulating browser state.
2. Users cannot perform actions outside their permissions.
3. Duplicate clicks do not create duplicate records.
4. Repeated safe submissions do not cause duplicate UI results.
5. Loading states prevent accidental repeated interactions where appropriate.
6. Stale data does not overwrite newer data unexpectedly.
7. Concurrent browser interactions do not produce inconsistent visible state.
8. Resource status transitions follow the expected workflow.
9. Restricted status transitions are rejected.
10. User-controlled IDs do not override resource ownership.
11. Destructive-action confirmation is required where applicable.
12. Typed confirmation is validated for high-impact actions, where implemented.
13. Idempotency behaviour is respected where safely observable.
Do not trigger real payments, account deletions, irreversible operations, or actual message delivery. Use mocked endpoints or disposable fixtures for potentially sensitive workflows.
If the page supports file uploads or downloads, test:
1. Disallowed file extensions.
2. Mismatched file extensions and MIME types.
3. Empty files.
4. Oversized files using safe test fixtures.
5. Invalid file signatures.
6. Unsupported file types.
7. Filenames containing HTML/script-like strings.
8. Filenames containing path traversal patterns.
9. Unicode and unusually long filenames.
10. Unauthorized access to another user's file.
11. Unauthorized file previews or downloads.
12. Safe handling of upload errors.
13. File upload progress and cancellation.
14. Download filenames and content types.
15. Uploaded content is not rendered as executable HTML or script.
16. File access follows the correct ownership and permission rules.
Do not upload malware, executable payloads, or genuinely dangerous files. Use harmless test fixtures.
If the page accepts URLs, external links, image URLs, webhook URLs, import URLs, or similar values, test:
1. Invalid URL formats.
2. Unsupported schemes.
3. JavaScript URL schemes.
4. Unexpected ports.
5. URL encoding edge cases.
6. Localhost and private-network destination rejection where observable.
7. Cloud metadata address rejection where applicable and safely testable.
8. External link handling and safe target="_blank" behaviour.
9. Appropriate rel="noopener noreferrer" protection.
10. Redirect handling and unexpected external navigation.
Do not attempt to access real internal services, cloud metadata endpoints, or private infrastructure. Use controlled mocks and safe validation checks.
Test:
1. User-generated HTML is not executed.
2. DOM-based XSS does not occur through search, filters, URL parameters, or rendered data.
3. Second-order XSS does not occur when previously stored test content is displayed elsewhere on the page.
4. Clipboard-pasted content is handled safely.
5. Untrusted links cannot trigger unintended navigation.
6. Sensitive information is not exposed through console errors.
7. Authentication state is not accidentally shared between browser contexts.
8. Restricted UI elements cannot be accessed by changing browser-side state.
9. Error boundaries prevent a single malformed record from crashing the entire page.
10. Browser back/forward navigation does not restore an unauthorized authenticated view after logout.
Do not inspect server log files or infrastructure monitoring systems.

Where browser-visible behaviour or safe mock endpoints permit it, test:
1. User-controlled input containing newline or carriage-return characters does not corrupt the rendered UI.
2. Sensitive values are not printed in the browser console.
3. Authentication and authorization errors do not expose secrets.
4. Sensitive actions produce the expected user-visible confirmation or audit reference where the page exposes one.
5. Failed operations do not display internal logging information.
Log injection, server-side audit completeness, centralized logging, log rotation, and log retention cannot be verified through ordinary browser-only Playwright tests. Do not invent tests for them.
Generate meaningful usability tests for every relevant component.

Cover:
1. Every visible button performs the expected action.
2. Every input has an understandable label or accessible name.
3. Required fields are clearly indicated.
4. Invalid input produces clear validation messages.
5. Validation messages identify the relevant field.
6. Errors remain visible long enough to be understood.
7. Successful actions provide appropriate feedback.
8. Loading states are visible and prevent accidental duplicate interactions.
9. Empty states explain what the user should do next.
10. Disabled controls are visibly and semantically disabled.
11. Tooltips and help text are accessible and meaningful.
12. Search fields handle no-result scenarios.
13. Filters work individually and in combination.
14. Clearing filters restores the expected results.
15. Pagination works at first, middle, and last pages.
16. Sorting works correctly.
17. Navigation preserves relevant state where expected.
18. Refreshing the page does not unexpectedly lose important state.
19. Browser back and forward navigation work correctly.
20. Long text does not make important controls inaccessible.
21. Unicode, multilingual text, and bidirectional text render safely.
22. Keyboard-only navigation works.
23. Tab order is logical.
24. Focus indicators are visible.
25. Modals trap focus appropriately.
26. Escape closes dismissible dialogs.
27. Focus returns to the triggering element after a modal closes.
28. Screen-reader accessible names are meaningful.
29. Images have appropriate alternative text.
30. Color is not the only way to communicate status.
31. Contrast is sufficient for important text and controls.
32. Reduced-motion preferences are respected.
33. Browser zoom does not make essential content unusable.
34. Responsive layouts work on desktop, tablet, and mobile viewports.
35. Touch targets are usable on mobile.
36. Dropdowns and menus do not overflow the viewport.
37. Long labels and content do not overlap other elements.
38. Network errors produce recoverable feedback.
39. Empty, loading, success, and failure states are consistent.
40. Password fields support copy/paste and password managers.
Do not generate a separate test for every trivial pixel or text variation. Focus on meaningful accessibility and usability outcomes.
Inspect the page's actual features and generate tests for all applicable workflows.

Consider:
1. Initial page rendering.
2. Correct page title and heading.
3. Correct initial data.
4. Correct user-specific data.
5. Correct role-specific data.
6. CRUD-related UI behaviour, using safe disposable test data.
7. Search and filtering correctness.
8. Sorting and pagination correctness.
9. Form validation.
10. Required and optional field behaviour.
11. Date and time validation.
12. Timezone-related behaviour where applicable.
13. Numeric boundaries.
14. Minimum and maximum values.
15. Decimal and rounding behaviour.
16. Status and workflow transitions.
17. Empty and null values.
18. Duplicate values.
19. Stale data and refresh behaviour.
20. API loading, success, and error responses.
21. Network timeout handling.
22. Unexpected API response structures.
23. Missing optional response fields.
24. Malformed records.
25. Correct navigation and redirects.
26. Correct permission-dependent visibility.
27. Correct data persistence after refresh, where the workflow is safely testable.
28. Correct handling of expired authentication.
29. Correct handling of invalid resource IDs.
30. Correct handling of unavailable resources.
Test boundary conditions and realistic negative scenarios, not just the happy path.
Generate only checks that Playwright can reliably measure.

Consider:
1. Page remains usable after a recoverable API failure.
2. Page does not crash when an API returns an empty list.
3. Page does not crash when an API returns null optional fields.
4. Page handles malformed but safely mocked response data.
5. Page handles slow network responses.
6. Loading indicators appear during meaningful delays.
7. Repeated navigation does not cause duplicate event handling.
8. Repeated opening and closing of dialogs does not break the page.
9. Rapid but reasonable interactions do not cause inconsistent UI state.
10. Large lists render and paginate correctly.
11. Images handle failed loading.
12. Fonts and static assets handle unavailable responses gracefully.
13. No unexpected uncaught JavaScript errors occur.
14. No unexpected failed critical API requests occur.
15. Layout remains stable during normal loading.
16. Navigation does not produce obvious broken states.
17. The page remains usable after a temporary network interruption.
Do not invent strict performance thresholds unless the project provides explicit requirements.
Generate only a few high-value visual checks.

Consider:
1. Major layout sections are visible.
2. Primary actions are not hidden or clipped.
3. No overlapping elements.
4. No broken images or icons.
5. No major horizontal overflow.
6. Important text is readable.
7. Responsive layout does not break at representative viewport sizes.
8. Modals and dropdowns remain within the viewport.
Avoid excessive snapshot tests, arbitrary pixel comparisons, and tests that fail because of harmless styling changes.
Every test case must:
- Be specific to the provided page.
- Have a clear security, usability, correctness, or robustness objective.
- Be deterministic and repeatable.
- Be non-destructive.
- Avoid real external side effects.
- Have meaningful assertions.
- Avoid duplicate coverage.
- Avoid trivial variations of an existing test.
- Use stable selectors and accessible locators where possible.
- Avoid arbitrary timeouts and unnecessary sleeps.
- Mock external dependencies when necessary.
- Clean up any disposable test data created by the test.
- Avoid assuming that a hidden UI element means backend authorization is secure.
- Verify backend authorization through safe GET requests when an accessible API endpoint exists.
For each test, clearly identify:
1. Test case title.
2. Ownership prefix (Devops:, Shared:, or Dev:).
3. Priority (Critical, High, Medium, or Low).
4. Preconditions and required test data.
5. Test steps.
6. Expected result.
7. Playwright implementation feasibility.
Use the ownership prefix directly before the test case title, for example:

Dev: Verify that a user cannot access another user's private project by changing the project ID in the URL.

Devops: Verify that the production response redirects HTTP traffic to HTTPS.

Shared: Verify that a foreign origin cannot access credentialed API responses through an incorrect CORS configuration.

Do not assign multiple ownership prefixes to one test. Select the primary responsible team.
Organize the generated test cases in this order:
1. Critical Security Tests.
2. High-Priority Security Tests.
3. Authentication, Authorization & Session Tests.
4. Input Validation & Injection Tests.
5. Data Privacy & Exposure Tests.
6. Infrastructure-Observable Security Tests.
7. Usability & Accessibility Tests.
8. Functional & Business Logic Tests.
9. Robustness & Error Handling Tests.
10. Performance-Related Browser Tests.
11. Low-Priority Visual Tests.
Within each section, order test cases by risk and practical value.

At the end, include:
- Number of generated test cases.
- Number of Dev tests.
- Number of Devops tests.
- Number of Shared tests.
- Number of Critical, High, Medium, and Low priority tests.
List only security controls relevant to this page that cannot genuinely be verified through Playwright. Briefly explain why they require backend, database, infrastructure, CI/CD, or operational verification.
List any specific test cases that could not be generated because required routes, roles, fixtures, or API details were unavailable. Do not invent endpoints or test accounts.
Identify the most important security boundaries and user workflows that deserve manual review if automated browser coverage is insufficient.
Be exhaustive but intelligent.

Generate the largest useful set of distinct Playwright test cases justified by the actual page. Put security and usability first. Include edge cases and negative scenarios. Avoid redundant tests and unsupported infrastructure checks.

Never confuse a frontend restriction with backend security. Never confuse a browser-observable security signal with proof that the underlying infrastructure is secure.
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

