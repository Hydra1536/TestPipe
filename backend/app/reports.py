"""Report exporters: JSON, CSV, standalone HTML, and PDF."""
import csv
import html
import io
import json

from .config import settings
from .models import TestCase, TestRun


def to_json(run: TestRun, cases: list[TestCase]) -> str:
    return json.dumps(
        {"run": run.model_dump(mode="json"), "test_cases": [c.model_dump(mode="json") for c in cases]},
        indent=2,
        ensure_ascii=False,
    )


def to_csv(run: TestRun, cases: list[TestCase]) -> str:
    by_id = {c.id: c for c in cases}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Test ID", "Title", "Type", "Page", "Page URL", "Expected Outcome",
                "Result", "Duration (ms)", "Error", "Logs"])
    for r in run.results:
        tc = by_id.get(r.test_id)
        page_url = r.page_url or (tc.page_url or tc.target_url if tc else "")
        w.writerow([
            r.test_id, r.title, r.test_type,
            r.page_title or (tc.page_title if tc else ""), page_url,
            tc.expected_outcome if tc else "",
            r.status.value, r.duration_ms, r.error or "", "\n".join(r.logs),
        ])
    return buf.getvalue()


_COLORS = {"Pass": "#15803d", "Fail": "#b91c1c", "Error": "#b45309"}


def to_html(run: TestRun, cases: list[TestCase], base_url: str = "") -> str:
    e = html.escape
    by_id = {c.id: c for c in cases}
    rows = []
    for r in run.results:
        tc = by_id.get(r.test_id)
        shot = f'<a href="{e(base_url + r.screenshot)}">screenshot</a>' if r.screenshot else ""
        page_url = r.page_url or (tc.page_url or tc.target_url if tc else "")
        page_label = r.page_title or (tc.page_title if tc else "") or page_url
        page_cell = f'<a href="{e(page_url)}" title="{e(page_url)}">{e(page_label)}</a>' if page_url else ""
        color = _COLORS[r.status.value]
        # Expected outcome with the actual Result shown right beneath it.
        expected_cell = (
            f"{e(tc.expected_outcome if tc else '')}"
            f'<div style="margin-top:4px;color:{color};font-weight:600">Result: {r.status.value}'
            f"{(' — ' + e(r.error)) if r.error else ''}</div>"
        )
        rows.append(
            f"<tr><td>{e(r.test_id)}</td><td>{e(r.title)}</td><td>{page_cell}</td><td>{e(r.test_type)}</td>"
            f"<td>{expected_cell}</td>"
            f'<td style="color:{color};font-weight:600">{r.status.value}</td>'
            f"<td>{r.duration_ms}</td>"
            f"<td><details><summary>{e(r.error or 'logs')}</summary><pre>{e(chr(10).join(r.logs))}</pre></details> {shot}</td></tr>"
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Test Report {e(run.run_id)}</title>
<style>
body{{font-family:system-ui,sans-serif;margin:2rem;color:#111}}
.cards{{display:flex;gap:1rem;margin:1rem 0}}
.card{{border:1px solid #ddd;border-radius:8px;padding:.75rem 1.25rem}}
.card b{{display:block;font-size:1.5rem}}
table{{border-collapse:collapse;width:100%;font-size:14px}}
th,td{{border:1px solid #e5e5e5;padding:6px 8px;text-align:left;vertical-align:top}}
th{{background:#f5f5f5}} pre{{white-space:pre-wrap;font-size:12px}}
</style></head><body>
<h1>Test Report</h1>
<p>Run <code>{e(run.run_id)}</code> &middot; {e(run.started_at)} &rarr; {e(run.finished_at)}</p>
<div class="cards">
<div class="card">Total<b>{run.total}</b></div>
<div class="card" style="color:{_COLORS['Pass']}">Passed<b>{run.passed}</b></div>
<div class="card" style="color:{_COLORS['Fail']}">Failed<b>{run.failed}</b></div>
<div class="card" style="color:{_COLORS['Error']}">Errors<b>{run.errors}</b></div>
</div>
<table><thead><tr><th>ID</th><th>Title</th><th>Page</th><th>Type</th><th>Expected &amp; Result</th>
<th>Status</th><th>ms</th><th>Details</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""


def to_pdf(run: TestRun, cases: list[TestCase]) -> bytes:
    """Render the HTML report to PDF using headless Chromium (already installed for
    scraping/execution). Screenshots aren't embedded; the HTML report keeps those links."""
    from playwright.sync_api import sync_playwright

    doc = to_html(run, cases)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=settings.headless)
        try:
            page = browser.new_page()
            page.set_content(doc, wait_until="load")
            pdf = page.pdf(format="A4", print_background=True,
                           margin={"top": "1cm", "bottom": "1cm", "left": "1cm", "right": "1cm"})
        finally:
            browser.close()
    return pdf
