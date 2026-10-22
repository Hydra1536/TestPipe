"""JSON-file persistence. Small, thread-safe, atomic writes — enough for an MVP."""
import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from .config import settings
from .models import PageSnapshot, SiteCrawl, TestCase, TestRun

TEST_CASES_FILE = settings.data_dir / "test_cases.json"
RUNS_FILE = settings.data_dir / "test_runs.json"
CRAWL_FILE = settings.data_dir / "last_crawl.json"
GEN_CACHE_FILE = settings.data_dir / "gen_cache.json"
MAX_RUNS_KEPT = 20
MAX_GEN_CACHE = 500

_lock = threading.RLock()


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ---- Test cases -------------------------------------------------------------

def list_test_cases() -> list[TestCase]:
    with _lock:
        return [TestCase(**tc) for tc in _read(TEST_CASES_FILE, [])]


def save_test_cases(cases: list[TestCase]) -> None:
    with _lock:
        _write(TEST_CASES_FILE, [c.model_dump(mode="json") for c in cases])


def get_test_case(test_id: str) -> Optional[TestCase]:
    return next((c for c in list_test_cases() if c.id == test_id), None)


def next_test_id(existing: list[TestCase]) -> str:
    nums = [int(m.group(1)) for c in existing if (m := re.fullmatch(r"TC-(\d+)", c.id))]
    return f"TC-{(max(nums) if nums else 0) + 1:03d}"


def append_test_cases(new_cases: list[TestCase]) -> list[TestCase]:
    """Assigns fresh IDs to the given cases and appends them."""
    with _lock:
        cases = list_test_cases()
        for c in new_cases:
            c.id = next_test_id(cases)
            cases.append(c)
        save_test_cases(cases)
        return new_cases


def replace_ai_test_cases(new_cases: list[TestCase]) -> list[TestCase]:
    with _lock:
        save_test_cases([c for c in list_test_cases() if c.source != "ai"])
        return append_test_cases(new_cases)


def update_test_case(test_id: str, changes: dict) -> Optional[TestCase]:
    with _lock:
        cases = list_test_cases()
        for i, c in enumerate(cases):
            if c.id == test_id:
                cases[i] = c.model_copy(update=changes)
                # re-validate so nested steps become TestStep objects
                cases[i] = TestCase(**cases[i].model_dump(mode="json"))
                save_test_cases(cases)
                return cases[i]
        return None


def delete_test_case(test_id: str) -> bool:
    with _lock:
        cases = list_test_cases()
        remaining = [c for c in cases if c.id != test_id]
        if len(remaining) == len(cases):
            return False
        save_test_cases(remaining)
        return True


# ---- Runs -------------------------------------------------------------------

def save_run(run: TestRun) -> None:
    with _lock:
        runs = _read(RUNS_FILE, [])
        runs.insert(0, run.model_dump(mode="json"))
        _write(RUNS_FILE, runs[:MAX_RUNS_KEPT])


def list_runs() -> list[TestRun]:
    with _lock:
        return [TestRun(**r) for r in _read(RUNS_FILE, [])]


def get_run(run_id: Optional[str] = None) -> Optional[TestRun]:
    runs = list_runs()
    if not runs:
        return None
    if run_id is None:
        return runs[0]
    return next((r for r in runs if r.run_id == run_id), None)


# ---- Last crawl (whole site; gives Groq selectors when compiling manual tests) --

def save_site_crawl(crawl: SiteCrawl) -> None:
    with _lock:
        _write(CRAWL_FILE, crawl.model_dump(mode="json"))


def get_site_crawl() -> Optional[SiteCrawl]:
    with _lock:
        data = _read(CRAWL_FILE, None)
        if not data:
            return None
        try:
            return SiteCrawl(**data)
        except (TypeError, ValueError):
            return None  # e.g. a crawl file written by an older version


def get_snapshot(url: Optional[str] = None) -> Optional[PageSnapshot]:
    """A single page from the last crawl: the one matching `url`, else the start page."""
    crawl = get_site_crawl()
    if not crawl or not crawl.pages:
        return None
    if url:
        match = next((p for p in crawl.pages if p.final_url == url or p.url == url), None)
        if match:
            return match
    return crawl.pages[0]


def reset_all() -> None:
    """Delete everything the dashboard shows. The generation cache is kept: it holds no
    visible content and saves Groq tokens on the next crawl."""
    with _lock:
        for f in (TEST_CASES_FILE, RUNS_FILE, CRAWL_FILE, settings.auth_state_file):
            f.unlink(missing_ok=True)
        for shot in settings.screenshots_dir.glob("*.png"):
            shot.unlink(missing_ok=True)


# ---- Generation cache (page-context hash -> generated test dicts) -------------

def get_cached_generation(key: str) -> Optional[list[dict]]:
    with _lock:
        return _read(GEN_CACHE_FILE, {}).get(key)


def put_cached_generation(key: str, cases: list[dict]) -> None:
    with _lock:
        cache = _read(GEN_CACHE_FILE, {})
        cache.pop(key, None)
        cache[key] = cases
        _write(GEN_CACHE_FILE, dict(list(cache.items())[-MAX_GEN_CACHE:]))  # keep newest
