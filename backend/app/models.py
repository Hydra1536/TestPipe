from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, HttpUrl


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StepAction(str, Enum):
    goto = "goto"                      # value = absolute or relative URL
    go_back = "go_back"                # browser back button
    reload = "reload"                  # reload current page
    click = "click"                    # selector
    fill = "fill"                      # selector + value
    press = "press"                    # selector (optional) + value = key, e.g. "Enter"
    select = "select"                  # selector + value = option value/label
    check = "check"                    # selector
    hover = "hover"                    # selector
    wait = "wait"                      # value = milliseconds (capped)
    expect_visible = "expect_visible"  # selector
    expect_hidden = "expect_hidden"    # selector
    expect_text = "expect_text"        # selector (optional, defaults to body) + value
    expect_value = "expect_value"      # selector + value (input value)
    expect_title = "expect_title"      # value = substring of <title>
    expect_url = "expect_url"          # value = substring of current URL
    expect_url_not = "expect_url_not"  # value = substring that must NOT be in the URL
    expect_not_text = "expect_not_text"  # like expect_text, but the text must NOT appear
    expect_no_dialog = "expect_no_dialog"  # no alert/confirm/prompt fired (XSS check)
    # HTTP checks with the browser's cookies. Read-only methods only (GET/HEAD/OPTIONS).
    request = "request"                # value = "METHOD url"; selector = optional JSON headers
    expect_status = "expect_status"    # value = "200", "403" or a class like "4xx"
    expect_header = "expect_header"    # selector = header name, value = optional substring
    expect_header_absent = "expect_header_absent"  # selector = header name


ASSERTION_ACTIONS = {a for a in StepAction if a.value.startswith("expect_")}

# Test categories the AI is asked to cover. Security & Scalability are first-class.
TEST_TYPES = [
    "Smoke",
    "Functional",
    "Usability",
    "UI",
    "Regression",
    "Navigation",
    "Performance",
    "Form Validation",
    "Accessibility",
    "Security",
    "Scalability",
    "Manual",
]


class TestStep(BaseModel):
    action: StepAction
    selector: Optional[str] = None
    value: Optional[str] = None


class TestStatus(str, Enum):
    passed = "Pass"
    failed = "Fail"
    error = "Error"


class TestCase(BaseModel):
    id: str
    title: str
    description: str = ""
    action_steps: list[str] = Field(default_factory=list)  # human-readable steps
    expected_outcome: str = ""
    test_type: str = "Functional"
    source: Literal["ai", "manual"] = "ai"
    target_url: str                  # the URL the test runs against
    page_url: str = ""               # which discovered page this test belongs to
    page_title: str = ""             # human label for that page
    requires_auth: bool = False      # page was only reachable after login/signup
    steps: list[TestStep] = Field(default_factory=list)  # machine-executable steps
    enabled: bool = True
    created_at: str = Field(default_factory=utcnow)


# ---- Auth -------------------------------------------------------------------

class AuthConfig(BaseModel):
    """How to reach pages behind a login. Intended for sites you own or are
    authorized to test."""
    mode: Literal["none", "signup", "login", "session"] = "none"  # session = reuse a manual login
    login_url: Optional[HttpUrl] = None  # page with the login/signup form; defaults to start URL
    email: Optional[str] = None          # for login; for signup, used if given else auto-generated
    username: Optional[str] = None
    password: Optional[str] = None       # for login; for signup, used if given else auto-generated


class AuthReport(BaseModel):
    attempted: bool = False
    mode: str = "none"
    success: bool = False
    detail: str = ""
    logged_in_as: Optional[str] = None  # email/username actually used
    logs: list[str] = Field(default_factory=list)


# ---- Request bodies ---------------------------------------------------------

class CrawlRequest(BaseModel):
    url: HttpUrl
    max_pages: int = Field(default=50, ge=1, le=500)
    max_tests_per_page: Optional[int] = Field(default=None, ge=1, le=200)  # None = no cap
    replace_existing_ai: bool = True  # drop previous AI tests, keep manual ones
    same_domain_only: bool = True
    auth: AuthConfig = Field(default_factory=AuthConfig)


class ManualLoginRequest(BaseModel):
    login_url: HttpUrl


class ManualTestCaseCreate(BaseModel):
    title: str = Field(min_length=1)
    description: str = ""
    action_steps: list[str] = Field(min_length=1)
    expected_result: str = Field(min_length=1)
    test_type: str = "Manual"
    target_url: Optional[HttpUrl] = None  # defaults to last crawled URL
    # Power users may supply executable steps directly; otherwise Groq compiles them.
    steps: Optional[list[TestStep]] = None


class TestCaseUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    action_steps: Optional[list[str]] = None
    expected_outcome: Optional[str] = None
    test_type: Optional[str] = None
    steps: Optional[list[TestStep]] = None
    enabled: Optional[bool] = None


class RunTestsRequest(BaseModel):
    test_ids: Optional[list[str]] = None  # None = all enabled tests


# ---- Results ----------------------------------------------------------------

class TestResult(BaseModel):
    test_id: str
    title: str
    test_type: str
    source: str
    page_url: str = ""
    page_title: str = ""
    status: TestStatus
    duration_ms: int
    logs: list[str] = Field(default_factory=list)
    error: Optional[str] = None
    screenshot: Optional[str] = None  # URL path under /static


class TestRun(BaseModel):
    run_id: str
    started_at: str
    finished_at: str
    total: int
    passed: int
    failed: int
    errors: int
    results: list[TestResult]


class PageElement(BaseModel):
    kind: str  # input | button | link | select | textarea | form
    selector: str
    tag: str
    text: str = ""
    attrs: dict[str, str] = Field(default_factory=dict)
    visible: bool = True


class PageSnapshot(BaseModel):
    url: str
    final_url: str
    title: str
    headings: list[str]
    elements: list[PageElement]
    links: list[str] = Field(default_factory=list)   # absolute hrefs found on the page
    api_calls: list[str] = Field(default_factory=list)  # "METHOD url" of XHR/fetch seen on load
    storage_keys: list[str] = Field(default_factory=list)  # localStorage key names (never values)
    requires_auth: bool = False
    screenshot: Optional[str] = None
    crawled_at: str = Field(default_factory=utcnow)


class SiteCrawl(BaseModel):
    """Result of crawling a whole site (one or more pages)."""
    start_url: str
    pages: list[PageSnapshot] = Field(default_factory=list)
    auth: AuthReport = Field(default_factory=AuthReport)
    crawled_at: str = Field(default_factory=utcnow)
