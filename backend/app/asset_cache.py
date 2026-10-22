"""Disk cache for immutable static assets (content-hashed JS/CSS/fonts).

Playwright starts every browser context with an empty HTTP cache, so a heavy SPA re-downloads
all its bundles on every crawl and every test — on a slow link that is 40s+ before the page
renders. Hashed filenames (Vite/webpack, e.g. `index-C6noYor9.js`) never change content, so
they are safe to serve from disk across contexts, crawls and test runs. Anything without a
hash falls through to the network, so a new deployment is never tested against stale code.
"""
import hashlib
import mimetypes
import re
import threading
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Route

from .config import settings

CACHE_DIR = settings.data_dir / "asset_cache"
# ponytail: unbounded dir; add a size cap if it ever grows large.

_HASHED = re.compile(r"[.-][A-Za-z0-9_]{8,}\.(?:m?js|css|woff2?|ttf|otf)$")
_IMMUTABLE_HOSTS = {"fonts.gstatic.com"}


def _cacheable(url: str) -> bool:
    p = urlparse(url)
    return bool(_HASHED.search(p.path.rsplit("/", 1)[-1])) or p.hostname in _IMMUTABLE_HOSTS


def _handle(route: Route) -> None:
    req = route.request
    if req.method != "GET" or not _cacheable(req.url):
        route.fallback()  # let other handlers (e.g. resource blocking) or the network take it
        return
    path = CACHE_DIR / hashlib.sha1(req.url.encode()).hexdigest()
    if path.exists():
        route.fulfill(
            status=200,
            body=path.read_bytes(),
            content_type=mimetypes.guess_type(urlparse(req.url).path)[0] or "application/octet-stream",
            headers={"access-control-allow-origin": "*", "cache-control": "max-age=31536000, immutable"},
        )
        return
    try:
        resp = route.fetch()
    except PlaywrightError:
        route.fallback()
        return
    body = resp.body()
    if resp.status == 200:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
        tmp.write_bytes(body)
        tmp.replace(path)  # atomic: parallel workers never read a half-written file
    route.fulfill(response=resp, body=body)


def install(context: BrowserContext) -> None:
    context.route("**/*.{js,mjs,css,woff,woff2,ttf,otf}", _handle)
