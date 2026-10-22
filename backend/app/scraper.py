"""Playwright crawler.

Crawls a whole site: starting from one URL it optionally signs up / logs in, then
walks internal links breadth-first (up to a caller-set page limit), extracting the
interactive elements of every page and taking a screenshot of each. The browser
session (cookies + localStorage) is shared across the whole crawl and saved to disk
so the test executor can reuse it for pages behind a login.

Uses the *sync* Playwright API. FastAPI runs sync work in a thread pool, which also
sidesteps the Windows event-loop issue where async Playwright can't spawn a browser
under uvicorn's selector loop.
"""
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import deque
from urllib.parse import urldefrag, urlparse

from playwright.sync_api import Browser, BrowserContext, Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from . import asset_cache
from .config import settings
from .models import AuthConfig, AuthReport, PageElement, PageSnapshot, SiteCrawl

log = logging.getLogger(__name__)

MAX_PER_KIND = 40
MAX_LINKS_PER_PAGE = 300
MAX_API_CALLS = 30

# URL suffixes that are downloads / assets, not pages worth crawling.
SKIP_SUFFIXES = (
    ".pdf", ".zip", ".rar", ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".webp", ".ico", ".mp4", ".mp3", ".avi", ".mov", ".css", ".js", ".json", ".xml",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".woff", ".woff2", ".ttf",
)
# Link text / hrefs that would end the session — never follow while crawling.
LOGOUT_MARKERS = ("logout", "log-out", "log out", "signout", "sign-out", "sign out")

# Runs in the browser. Builds a reasonably stable CSS/Playwright selector per element.
EXTRACT_JS = r"""
(maxPerKind) => {
  const q = (s) => s.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const text = (el) => (el.innerText || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 80);

  const cssPath = (el) => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body && parts.length < 5) {
      if (el.id) { parts.unshift('#' + CSS.escape(el.id)); break; }
      const tag = el.tagName.toLowerCase();
      const sibs = el.parentElement ? [...el.parentElement.children].filter(c => c.tagName === el.tagName) : [];
      parts.unshift(sibs.length > 1 ? `${tag}:nth-of-type(${sibs.indexOf(el) + 1})` : tag);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };

  const selectorFor = (el) => {
    const tag = el.tagName.toLowerCase();
    if (el.id) return '#' + CSS.escape(el.id);
    for (const a of ['data-testid', 'data-test', 'data-cy']) {
      const v = el.getAttribute(a);
      if (v) return `[${a}="${q(v)}"]`;
    }
    const name = el.getAttribute('name');
    if (name) return `${tag}[name="${q(name)}"]`;
    const aria = el.getAttribute('aria-label');
    if (aria) return `${tag}[aria-label="${q(aria)}"]`;
    const ph = el.getAttribute('placeholder');
    if (ph) return `${tag}[placeholder="${q(ph)}"]`;
    const t = text(el);
    if ((tag === 'button' || tag === 'a') && t && t.length <= 50) return `${tag}:has-text("${q(t)}")`;
    if (tag === 'a' && el.getAttribute('href')) return `a[href="${q(el.getAttribute('href'))}"]`;
    return cssPath(el);
  };

  const pick = (el, keys) => {
    const out = {};
    for (const k of keys) { const v = el.getAttribute(k); if (v) out[k] = v.slice(0, 120); }
    return out;
  };

  const groups = {
    input: ['input:not([type=hidden])', ['type', 'name', 'placeholder', 'required', 'aria-label', 'value']],
    textarea: ['textarea', ['name', 'placeholder', 'required', 'aria-label']],
    select: ['select', ['name', 'required', 'aria-label']],
    button: ['button, input[type=submit], input[type=button], [role=button]', ['type', 'name', 'aria-label']],
    link: ['a[href]', ['href', 'target', 'aria-label']],
    form: ['form', ['action', 'method', 'name']],
  };

  const out = [];
  for (const [kind, [sel, keys]] of Object.entries(groups)) {
    const els = [...document.querySelectorAll(sel)];
    els.sort((a, b) => visible(b) - visible(a));  // visible elements first
    for (const el of els.slice(0, maxPerKind)) {
      const attrs = pick(el, keys);
      if (kind === 'select') attrs.options = [...el.options].slice(0, 10).map(o => o.text.trim()).join(' | ');
      out.push({
        kind,
        tag: el.tagName.toLowerCase(),
        selector: selectorFor(el),
        text: kind === 'form' ? '' : text(el),
        attrs,
        visible: visible(el),
      });
    }
  }
  const headings = [...document.querySelectorAll('h1, h2, h3')]
    .map(h => h.innerText.trim()).filter(Boolean).slice(0, 20);
  const links = [...new Set([...document.querySelectorAll('a[href]')].map(a => a.href).filter(Boolean))].slice(0, 300);
  return { elements: out, headings, links };
}
"""


class CrawlError(Exception):
    pass


# Text shown by common bot-protection interstitials (Cloudflare, etc.)
CHALLENGE_MARKERS = (
    "performing security verification",
    "verify you are human",
    "checking your browser",
    "just a moment...",
    "attention required! | cloudflare",
)


def _is_challenge_page(title: str, body_text: str) -> bool:
    haystack = f"{title}\n{body_text[:2000]}".lower()
    return any(m in haystack for m in CHALLENGE_MARKERS)


def _normalize(url: str) -> str:
    """Drop the fragment and a trailing slash so the same page isn't crawled twice."""
    url, _ = urldefrag(url)
    if url.endswith("/") and urlparse(url).path != "/":
        url = url[:-1]
    return url


def _same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def _crawlable(url: str) -> bool:
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return False
    if any(p.path.lower().endswith(s) for s in SKIP_SUFFIXES):
        return False
    if any(m in url.lower() for m in LOGOUT_MARKERS):
        return False
    return True


# ---- Low-level page extraction ---------------------------------------------

INTERACTIVE_SELECTOR = "a[href], button, input, select, textarea, [role=button], [role=link]"


def wait_for_render(page: Page, timeout_ms: int) -> bool:
    """Wait until an SPA has rendered something interactive and its URL has settled.

    SPAs keep the whole layout behind a spinner until their JS chunks arrive (40s+ on a
    cold cache). Polling in Python rather than one wait_for_function survives client-side
    redirects (`/` -> `/dashboard`), which destroy the JS context mid-wait.
    Returns False if nothing rendered in time."""
    deadline = time.monotonic() + timeout_ms / 1000
    last_url = None
    while time.monotonic() < deadline:
        try:
            if page.locator(INTERACTIVE_SELECTOR).count() > 0 and page.url == last_url:
                return True
            last_url = page.url
        except PlaywrightError:
            last_url = None  # mid-navigation; try again
        page.wait_for_timeout(500)
    return False


def _goto(page: Page, url: str) -> None:
    # "commit" returns as soon as the server responds; a heavy auth-behind SPA can take
    # far longer to reach "domcontentloaded", which used to time out the whole crawl.
    page.goto(url, wait_until="commit", timeout=settings.nav_timeout_ms)
    wait_for_render(page, settings.render_timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except PlaywrightError:
        pass  # chatty sites never go idle; the rendered DOM is enough


def _snapshot(page: Page, requested_url: str, requires_auth: bool = False,
              api_calls: list[str] | None = None) -> PageSnapshot:
    if _is_challenge_page(page.title(), page.inner_text("body")):
        raise CrawlError(
            f"{requested_url} returned a bot-protection challenge page (e.g. Cloudflare) "
            "instead of the real site. Try again later, or test a site you control."
        )
    shot_name = f"crawl-{uuid.uuid4().hex[:10]}.png"
    data = page.evaluate(EXTRACT_JS, MAX_PER_KIND)
    if not data["elements"]:
        log.warning("%s rendered no interactive elements within %ds (still loading?); "
                    "raise RENDER_TIMEOUT_MS if this page is slow.", page.url,
                    settings.render_timeout_ms // 1000)
    try:
        page.screenshot(path=str(settings.screenshots_dir / shot_name), full_page=True, timeout=8000)
        screenshot = f"/static/screenshots/{shot_name}"
    except PlaywrightError:
        screenshot = None
    return PageSnapshot(
        url=requested_url,
        final_url=page.url,
        title=page.title(),
        headings=data["headings"],
        elements=[PageElement(**e) for e in data["elements"]],
        links=data["links"][:MAX_LINKS_PER_PAGE],
        api_calls=list(dict.fromkeys(api_calls or []))[:MAX_API_CALLS],
        storage_keys=page.evaluate("() => { try { return Object.keys(localStorage) } catch { return [] } }")[:30],
        requires_auth=requires_auth,
        screenshot=screenshot,
    )


# ---- SPA route discovery ----------------------------------------------------

# Whole labels that are not navigation (matched exactly, case-insensitive). Exact match
# matters: a substring check would wrongly drop "Consult" (contains "on"), "Notifications"
# (contains "no"), "Bookings" (contains "ok"), etc.
NAV_SKIP_EXACT = {
    "on", "off", "toggle", "menu", "close", "open", "cancel", "delete", "remove", "save",
    "submit", "confirm", "yes", "no", "ok", "apply", "search", "filter", "next", "previous",
}


# Discovery clicks buttons on a live, logged-in account, so never click anything that looks
# like it acts on data. Whole words only: "Payment" and "Consult" must still pass.
_ACTION_WORDS = re.compile(
    r"\b(delete|remove|cancel|claim|redeem|pay|purchase|buy|checkout|subscribe|unsubscribe|"
    r"book|confirm|submit|send|create|add|save|upload|reset|deactivate|mark|clear|approve|"
    r"reject|accept|decline|join|leave|block|report|end|stop|sync|connect|disconnect)\b"
)


def _is_nav_label(text: str) -> bool:
    """A clickable label worth trying as navigation."""
    low = text.strip().lower()
    if not low or len(text) > 40:
        return False
    if low in NAV_SKIP_EXACT or _ACTION_WORDS.search(low):
        return False
    # Logout/sign-out end the session — never follow (substring: "log out", "signout"...).
    if any(m in low for m in LOGOUT_MARKERS):
        return False
    return True


# Controls that may route somewhere. Tabs are included: some apps route per tab
# (/log-data/nutrition -> /log-data/exercise); ones that don't just don't change the URL.
_NAV_CANDIDATES = "button, [role=button], [role=tab]"


def _label(text: str) -> str:
    return " ".join(text.split())  # "Features\nAI" -> "Features AI"


def _discover_button_routes(context: BrowserContext, page_url: str, tried: set[str],
                            max_clicks: int = 40) -> list[str]:
    """Many SPAs navigate with <button onClick> instead of <a href>, so the link crawler
    finds nothing to feed the BFS queue. This finds a page's real neighbours: it clicks each
    nav-looking button and records the URL it routes to.

    Buttons are matched by their rendered text at click time, not by a CSS selector, because
    multi-line labels ("Features\\nAI") don't match `:has-text("Features AI")`. We go back to
    `page_url` only after a click actually navigated (or failed); a click that didn't navigate
    leaves us on the base page, so Escape closes any popover and we carry on — reloading before
    every button made discovery take minutes.

    `tried` is shared across the whole crawl: the sidebar is clicked once on the first page,
    and later pages only try their own sub-navigation."""
    found: list[str] = []
    page = context.new_page()
    base = _normalize(page_url)
    try:
        _goto(page, page_url)
        buttons = page.locator(_NAV_CANDIDATES)
        labels = [l for l in dict.fromkeys(_label(t) for t in buttons.all_inner_texts())
                  if _is_nav_label(l) and l not in tried]
        at_base = True
        for label in labels[:max_clicks]:
            tried.add(label)
            try:
                if not at_base:
                    _goto(page, page_url)
                    at_base = True
                texts = [_label(t) for t in buttons.all_inner_texts()]
                if label not in texts:
                    continue
                buttons.nth(texts.index(label)).click(timeout=4000)
            except PlaywrightError:
                at_base = False  # unknown state (modal? navigation?) — reload before the next one
                continue
            try:
                page.wait_for_url(lambda u: _normalize(u) != base, timeout=2000)
            except PlaywrightError:
                page.keyboard.press("Escape")  # didn't navigate; close any popover/menu it opened
                continue
            at_base = False
            url = _normalize(page.url)
            if _crawlable(url) and url not in found:
                found.append(url)
    except PlaywrightError as e:
        log.warning("SPA route discovery failed on %s: %s", page_url, e.message.splitlines()[0])
    finally:
        page.close()
    if found:
        log.info("Discovered %d button-nav route(s) from %s", len(found), page_url)
    return found


# ---- Authentication (best-effort, for sites you own / are authorized to test) --

SIGNUP_LINK_RE = r"sign\s*up|register|create.*account|get started|join"
LOGIN_LINK_RE = r"log\s*in|sign\s*in"


AUTH_URL_MARKERS = ("signin", "sign-in", "login", "log-in", "signup", "sign-up", "register", "/auth")
VERIFY_MARKERS = (
    "verify your email", "verification email", "check your inbox", "check your email",
    "confirm your email", "we sent", "activation link", "verify your account",
)


def _auth_outcome(page: Page, entry_url: str) -> tuple[bool, str]:
    """Best-effort check of whether we ended up in a logged-in session.

    Returns (success, detail). Confirming this on an arbitrary site is inherently
    unreliable, so the detail explains what was observed."""
    try:
        body = page.inner_text("body").lower()
    except PlaywrightError:
        body = ""
    cur = page.url.lower()

    # An explicit logout control is the strongest positive signal.
    try:
        for marker in ("Logout", "Log out", "Sign out", "Signout", "My account", "Dashboard"):
            if page.get_by_text(marker, exact=False).count() > 0:
                return True, f"A '{marker}' control is present, indicating a logged-in session."
    except PlaywrightError:
        pass

    # Email verification is a common, expected reason auto sign-up can't finish.
    if any(m in body for m in VERIFY_MARKERS):
        return False, ("The site appears to require email verification before the account is active, "
                       "so protected pages can't be reached automatically.")

    on_auth_page = any(m in cur for m in AUTH_URL_MARKERS)
    try:
        has_password = page.locator("input[type=password]:visible").count() > 0
    except PlaywrightError:
        has_password = False

    if not has_password and not on_auth_page:
        return True, "Left the login/sign-up page and no login form remains, so a session was likely established."
    if has_password:
        try:
            msgs = [t.strip() for t in page.locator(
                "[role=alert], [aria-live=assertive], .error, .alert-danger, .invalid-feedback, [class*=error i]"
            ).all_inner_texts() if t.strip()]
        except PlaywrightError:
            msgs = []
        site_says = f" The site says: \"{msgs[0][:200]}\"" if msgs else ""
        return False, ("A password field is still shown after submitting, so login/sign-up likely failed "
                       "(validation error, wrong credentials, or extra required fields)." + site_says)
    return False, "Couldn't confirm a logged-in session; protected pages may still be inaccessible."


def _fill_auth_form(page: Page, email: str, password: str, name: str, log_lines: list[str]) -> None:
    """Fill whatever email / password / name / username fields are present, then submit."""
    # Scope to the form holding the password field, so a newsletter/search form elsewhere
    # on the page doesn't get the credentials or the submit click.
    form = page.locator("form:has(input[type=password])").first
    scope = form if form.count() else page

    def fill_first(selector: str, value: str, label: str) -> bool:
        loc = scope.locator(selector).first
        try:
            if loc.count() and loc.is_visible():
                loc.fill(value)
                log_lines.append(f"filled {label}")
                return True
        except PlaywrightError:
            pass
        return False

    fill_first("input[type=email], input[name*=email i], input[id*=email i]", email, "email")
    # username: a text input that isn't the email one
    fill_first(
        "input[name*=user i]:not([type=email]), input[id*=user i], input[name*=name i][type=text]",
        name,
        "username/name",
    )
    # password + a likely confirm-password field
    pwds = scope.locator("input[type=password]")
    try:
        for i in range(min(pwds.count(), 2)):
            pwds.nth(i).fill(password)
        if pwds.count():
            log_lines.append(f"filled {min(pwds.count(), 2)} password field(s)")
    except PlaywrightError:
        pass

    # submit
    submit = scope.locator(
        "button[type=submit], input[type=submit], "
        "button:has-text('Sign up'), button:has-text('Register'), "
        "button:has-text('Create'), button:has-text('Log in'), button:has-text('Sign in')"
    ).first
    before = page.url
    try:
        if submit.count():
            submit.click()
        else:
            pwds.last.press("Enter")
        log_lines.append("submitted form")
    except PlaywrightError as e:
        log_lines.append(f"submit issue: {e.message.splitlines()[0]}")
        return
    try:
        # SPAs go "network idle" before their client-side redirect, so wait until the URL
        # changes or the password field is gone. A timeout here means login likely failed.
        page.wait_for_function(
            "u => location.href !== u || ![...document.querySelectorAll('input[type=password]')]"
            ".some(e => e.offsetParent)",
            arg=before,
            timeout=8000,
        )
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightError:
        pass


def _attempt_auth(context: BrowserContext, start_url: str, auth: AuthConfig) -> AuthReport:
    report = AuthReport(attempted=True, mode=auth.mode)
    page = context.new_page()
    try:
        entry = str(auth.login_url) if auth.login_url else start_url
        _goto(page, entry)

        email = auth.email or f"qa_{secrets.token_hex(5)}@example.com"
        password = auth.password or f"Qa!{secrets.token_hex(6)}A9"
        name = auth.username or f"qa_{secrets.token_hex(3)}"

        if auth.mode == "signup":
            # If we're not already on a form with a password field, hunt for a signup link.
            if page.locator("input[type=password]").count() == 0:
                link = page.get_by_role("link", name=re.compile(SIGNUP_LINK_RE, re.I)).first
                try:
                    if link.count():
                        link.click()
                        page.wait_for_load_state("networkidle", timeout=8000)
                        report.logs.append("followed a sign-up link")
                except PlaywrightError:
                    pass
            report.logs.append(f"using generated identity {email}")
            _fill_auth_form(page, email, password, name, report.logs)

        elif auth.mode == "login":
            if not (auth.email or auth.username) or not auth.password:
                report.detail = "Login mode needs email/username and password."
                return report
            if page.locator("input[type=password]").count() == 0:
                link = page.get_by_role("link", name=re.compile(LOGIN_LINK_RE, re.I)).first
                try:
                    if link.count():
                        link.click()
                        page.wait_for_load_state("networkidle", timeout=8000)
                        report.logs.append("followed a log-in link")
                except PlaywrightError:
                    pass
            _fill_auth_form(page, auth.email or "", password, auth.username or "", report.logs)

        report.success, report.detail = _auth_outcome(page, entry)
        report.logged_in_as = email if auth.mode == "signup" else (auth.email or auth.username)
        # Persist the session for the crawl and later test runs.
        try:
            context.storage_state(path=str(settings.auth_state_file))
            report.logs.append("saved session state")
        except PlaywrightError:
            pass
        return report
    except (PlaywrightError, CrawlError) as e:
        report.detail = f"Auth flow failed: {getattr(e, 'message', str(e)).splitlines()[0]}"
        return report
    finally:
        page.close()


# ---- Public API -------------------------------------------------------------

def crawl_site(
    start_url: str,
    max_pages: int = 50,
    same_domain_only: bool = True,
    auth: AuthConfig | None = None,
    on_page=None,
) -> SiteCrawl:
    """`on_page(n)` is called after each captured page."""
    auth = auth or AuthConfig()
    use_session = auth.mode == "session"
    if use_session and not settings.auth_state_file.exists():
        raise CrawlError("No saved login session. Use \"Log in myself\" and finish logging in first.")
    if not use_session:
        # A fresh crawl starts a fresh session file.
        settings.auth_state_file.unlink(missing_ok=True)
    result = SiteCrawl(start_url=start_url)

    try:
        with sync_playwright() as p:
            browser: Browser = p.chromium.launch(headless=settings.headless)
            context = browser.new_context(
                viewport={"width": 1366, "height": 900},
                user_agent=settings.user_agent,
                storage_state=str(settings.auth_state_file) if use_session else None,
            )
            context.set_default_timeout(settings.step_timeout_ms)
            context.set_default_navigation_timeout(settings.nav_timeout_ms)
            asset_cache.install(context)  # hashed JS/CSS from disk after the first load
            try:
                authed = False
                if use_session:
                    authed = True
                    result.auth = AuthReport(attempted=True, mode="session", success=True,
                                             detail="Using the session you logged in with manually.")
                elif auth.mode != "none":
                    result.auth = _attempt_auth(context, start_url, auth)
                    authed = result.auth.success

                page = context.new_page()
                # XHR/fetch calls made while a page loads: real API endpoints for the
                # model's authorization / data-exposure tests.
                api_calls: list[str] = []
                page.on("request", lambda r: r.resource_type in ("xhr", "fetch") and api_calls.append(
                    f"{r.method} {r.url}" + (" [sends Authorization header]" if "authorization" in r.headers else "")))
                queue: deque[str] = deque([_normalize(start_url)])
                seen: set[str] = {_normalize(start_url)}
                seen_final: set[str] = set()  # dedupe pages that redirect to the same URL
                tried_labels: set[str] = set()  # button labels already clicked during discovery

                while queue and len(result.pages) < max_pages:
                    url = queue.popleft()
                    api_calls.clear()
                    try:
                        _goto(page, url)
                        snap = _snapshot(page, url, requires_auth=authed, api_calls=api_calls)
                    except CrawlError:
                        if not result.pages:
                            raise  # first page failing is fatal
                        log.warning("Skipping page (challenge/blocked): %s", url)
                        continue
                    except PlaywrightError as e:
                        if not result.pages:
                            raise CrawlError(f"Could not load {url}: {e.message.splitlines()[0]}") from e
                        log.warning("Skipping unreachable page %s: %s", url, e.message.splitlines()[0])
                        continue

                    final = _normalize(snap.final_url)
                    if final in seen_final:
                        continue  # a different URL redirected to a page we already captured
                    seen_final.add(final)
                    result.pages.append(snap)
                    if on_page:
                        on_page(len(result.pages))

                    hrefs = list(snap.links)
                    # SPA with button-based nav and few <a href> links: find this page's
                    # neighbours by clicking (labels tried on earlier pages are skipped).
                    if len(hrefs) < 3 and len(result.pages) < max_pages:
                        hrefs += _discover_button_routes(context, snap.final_url, tried_labels)

                    for href in hrefs:
                        nxt = _normalize(href)
                        if nxt in seen or not _crawlable(nxt):
                            continue
                        if same_domain_only and not _same_site(nxt, start_url):
                            continue
                        seen.add(nxt)
                        queue.append(nxt)
            finally:
                browser.close()
    except PlaywrightError as e:
        raise CrawlError(f"Could not load {start_url}: {e.message.splitlines()[0]}") from e

    if not result.pages:
        raise CrawlError(f"Could not extract any pages starting from {start_url}.")
    return result


MANUAL_LOGIN_TIMEOUT_S = 600


def manual_login(login_url: str, stop: threading.Event) -> dict:
    """Open a visible browser at `login_url` and let the user log in by hand (Google
    OAuth, 2FA, captcha...). Ends when `stop` is set, all tabs are closed, or on timeout,
    then saves the session once."""
    settings.auth_state_file.unlink(missing_ok=True)
    # Without these flags Google's sign-in page refuses automated browsers.
    launch = {"headless": False, "args": ["--disable-blink-features=AutomationControlled"],
              "ignore_default_args": ["--enable-automation"]}
    saved = False
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", **launch)  # real Chrome, if installed
        except PlaywrightError:
            browser = p.chromium.launch(**launch)
        try:
            context = browser.new_context(no_viewport=True)
            page = context.new_page()
            # "commit" returns as soon as the server responds, so a slow SPA doesn't time out.
            # A failure here is non-fatal: the window is open, the user can navigate/log in
            # themselves. Only a browser that won't open at all should stop us.
            try:
                page.goto(login_url, wait_until="commit", timeout=settings.nav_timeout_ms)
            except PlaywrightError as e:
                log.warning("manual login: initial navigation to %s failed (%s); "
                            "leaving the window open for the user.", login_url, e.message.splitlines()[0])
            # Don't call storage_state() in this loop: it opens a tab and navigates to every
            # visited origin to read localStorage, which the user sees as the page reloading.
            # context.cookies() is a plain RPC, so keep a cookie snapshot as a fallback in
            # case the whole browser gets closed.
            cookies: list = []
            deadline = time.monotonic() + MANUAL_LOGIN_TIMEOUT_S
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    cookies = context.cookies()  # also pumps browser events
                except PlaywrightError:
                    break  # browser closed
                if not context.pages:
                    break  # user closed every tab
                time.sleep(1)
            try:
                context.storage_state(path=str(settings.auth_state_file))
                saved = True
            except PlaywrightError:
                if cookies:
                    settings.auth_state_file.write_text(
                        json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")
                    saved = True
        finally:
            try:
                browser.close()
            except PlaywrightError:
                pass
    if not saved:
        raise CrawlError("The login window closed before a session could be saved.")
    return {"saved": True}


def crawl(url: str) -> PageSnapshot:
    """Single-page crawl (used to give manual-test compilation page context)."""
    return crawl_site(url, max_pages=1).pages[0]
