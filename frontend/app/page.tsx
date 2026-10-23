"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CrawlForm } from "@/components/CrawlForm";
import { CrawlSummaryPanel } from "@/components/CrawlSummaryPanel";
import { DownloadMenu } from "@/components/DownloadMenu";
import { SummaryCards } from "@/components/SummaryCards";
import { TestCaseTable } from "@/components/TestCaseTable";
import { Alert, Button, Card, ProgressBar } from "@/components/ui";
import { api } from "@/lib/api";
import type {
  CrawlOptions,
  CrawlSummary,
  Health,
  JobProgress,
  TestCase,
  TestResult,
  TestRun,
} from "@/lib/types";

function useElapsed(active: boolean) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!active) return;
    const start = Date.now();
    const id = setInterval(() => setSeconds(Math.floor((Date.now() - start) / 1000)), 500);
    return () => {
      clearInterval(id);
      setSeconds(0);
    };
  }, [active]);
  return seconds;
}

const ALL = "__all__";

export default function Dashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [testCases, setTestCases] = useState<TestCase[]>([]);
  const [run, setRun] = useState<TestRun | null>(null);
  const [crawl, setCrawl] = useState<CrawlSummary | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [crawling, setCrawling] = useState(false);
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [resetting, setResetting] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pageFilter, setPageFilter] = useState<string>(ALL);
  const [typeFilter, setTypeFilter] = useState<string>(ALL);
  const noticeTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const elapsed = useElapsed(running || crawling);

  const flash = useCallback((msg: string) => {
    setNotice(msg);
    clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 6000);
  }, []);

  const fail = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  useEffect(() => {
    Promise.all([api.health(), api.listTestCases(), api.latestRun(), api.latestCrawl()])
      .then(([h, tcs, r, c]) => {
        setHealth(h);
        setTestCases(tcs);
        setRun(r);
        setCrawl(c);
      })
      .catch(fail)
      .finally(() => setLoaded(true));
  }, []);

  const results = useMemo(
    () => new Map<string, TestResult>(run?.results.map((r) => [r.test_id, r]) ?? []),
    [run],
  );

  const pageOptions = useMemo(() => {
    const map = new Map<string, string>();
    for (const t of testCases) if (t.page_url) map.set(t.page_url, t.page_title || t.page_url);
    return [...map.entries()];
  }, [testCases]);

  const typeOptions = useMemo(
    () => [...new Set(testCases.map((t) => t.test_type))].sort(),
    [testCases],
  );

  const filtered = useMemo(
    () =>
      testCases.filter(
        (t) =>
          (pageFilter === ALL || t.page_url === pageFilter) &&
          (typeFilter === ALL || t.test_type === typeFilter),
      ),
    [testCases, pageFilter, typeFilter],
  );

  const enabledCount = testCases.filter((t) => t.enabled).length;

  async function handleCrawl(url: string, opts: CrawlOptions) {
    setCrawling(true);
    setError(null);
    try {
      const res = await api.crawl(url, opts, setProgress);
      setTestCases(res.test_cases);
      setCrawl(res.crawl);
      setPageFilter(ALL);
      setTypeFilter(ALL);
      const a = res.crawl.auth;
      const authNote =
        a.attempted && !a.success ? " Login could not be confirmed, so protected pages may be missing." : "";
      flash(
        `Crawled ${res.crawl.page_count} page(s) and generated ${res.generated_count} test cases.${authNote}`,
      );
    } catch (e) {
      fail(e);
    } finally {
      setCrawling(false);
      setProgress(null);
    }
  }

async function handleRun() {
    setRunning(true);
    setError(null);
    try {
      const activeTestIds = testCases.filter((t) => t.enabled).map((t) => t.id);

      const r = await api.runTests(activeTestIds, setProgress);
      setRun(r);
      flash(`Run finished: ${r.passed}/${r.total} passed, ${r.failed} failed, ${r.errors} errors.`);
    } catch (e) {
      fail(e);
    } finally {
      setRunning(false);
      setProgress(null);
    }
  }

  async function handleReset() {
    if (!confirm("Delete all test cases (including manual ones), run history, crawl data and the saved login?")) return;
    setResetting(true);
    setError(null);
    try {
      await api.reset();
      setTestCases([]);
      setRun(null);
      setCrawl(null);
      setPageFilter(ALL);
      setTypeFilter(ALL);
      flash("Everything was cleared.");
    } catch (e) {
      fail(e);
    } finally {
      setResetting(false);
    }
  }

  async function handleToggle(tc: TestCase) {
    setTestCases((prev) => prev.map((t) => (t.id === tc.id ? { ...t, enabled: !t.enabled } : t)));
    try {
      await api.updateTestCase(tc.id, { enabled: !tc.enabled });
    } catch (e) {
      setTestCases((prev) => prev.map((t) => (t.id === tc.id ? tc : t)));
      fail(e);
    }
  }

  async function handleDelete(tc: TestCase) {
    if (!confirm(`Delete ${tc.id} "${tc.title}"?`)) return;
    try {
      await api.deleteTestCase(tc.id);
      setTestCases((prev) => prev.filter((t) => t.id !== tc.id));
    } catch (e) {
      fail(e);
    }
  }

  const selectClass =
    "rounded-md border border-slate-300 bg-white px-2 py-1 text-sm shadow-sm focus:border-indigo-500 focus:outline-none";

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-4 py-8 sm:px-6">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900">VaiTestPipe</h1>
          <p className="text-sm text-slate-500">
            AI-generated frontend, security & scalability tests, executed with Playwright.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={handleReset} loading={resetting} disabled={crawling || running}>
            Refresh
          </Button>
        {health && (
          <span
            className={`rounded-full px-2.5 py-1 text-xs font-medium ${
              health.groq_configured ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-800"
            }`}
          >
            {health.groq_configured ? `AI ready · ${health.model}` : "GROQ_API_KEY not set"}
          </span>
        )}
        </div>
      </header>

      {error && <Alert onClose={() => setError(null)}>{error}</Alert>}
      {notice && (
        <div role="status" className="rounded-md border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          {notice}
        </div>
      )}

      <Card className="space-y-4 p-5">
        <div>
          <h2 className="font-semibold text-slate-900">1. Crawl a site & generate tests</h2>
          <p className="text-sm text-slate-500">
            The whole site is crawled page by page in a headless browser, and the AI writes tests for every
            page. Previous AI tests are replaced; manual tests are kept.
          </p>
        </div>
        <CrawlForm loading={crawling} disabled={running} onSubmit={handleCrawl} />
        {crawling && <ProgressBar progress={progress} elapsed={elapsed} />}
        {crawl && !crawling && <CrawlSummaryPanel crawl={crawl} />}
      </Card>

      <SummaryCards testCount={testCases.length} run={run} />

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div>
            <h2 className="font-semibold text-slate-900">2. Review & run test cases</h2>
            <p className="text-sm text-slate-500">
              {run
                ? `Last run ${new Date(run.finished_at).toLocaleString()}`
                : "Untick a test to skip it. Click a title for details."}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <DownloadMenu runId={run?.run_id} />
            <Button variant="primary" onClick={handleRun} loading={running} disabled={!enabledCount || crawling}>
              {running ? "Running…" : `Run Tests (${enabledCount})`}
            </Button>
          </div>
        </div>
        {running && (
          <div className="border-b border-slate-200 px-5 py-3">
            <ProgressBar progress={progress} elapsed={elapsed} />
          </div>
        )}

        {testCases.length > 0 && (
          <div className="flex flex-wrap items-center gap-3 border-b border-slate-200 bg-slate-50/60 px-5 py-2.5 text-sm">
            <label className="flex items-center gap-1.5 text-slate-600">
              Page
              <select value={pageFilter} onChange={(e) => setPageFilter(e.target.value)} className={selectClass}>
                <option value={ALL}>All ({pageOptions.length})</option>
                {pageOptions.map(([url, title]) => (
                  <option key={url} value={url}>
                    {title}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1.5 text-slate-600">
              Type
              <select value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)} className={selectClass}>
                <option value={ALL}>All</option>
                {typeOptions.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>
            <span className="text-xs text-slate-400">
              Showing {filtered.length} of {testCases.length}
            </span>
          </div>
        )}

        {loaded ? (
          <TestCaseTable
            testCases={filtered}
            results={results}
            running={running}
            onToggle={handleToggle}
            onDelete={handleDelete}
          />
        ) : (
          <p className="px-6 py-16 text-center text-sm text-slate-400">Loading…</p>
        )}
      </Card>
    </main>
  );
}
