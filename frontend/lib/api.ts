import type {
  CrawlOptions,
  CrawlResponse,
  CrawlSummary,
  Health,
  JobProgress,
  ReportFormat,
  TestCase,
  TestRun,
} from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

interface Job extends JobProgress {
  status: "running" | "done" | "error";
  result?: unknown;
  status_code?: number;
  detail?: string;
}

/** Polls a background job until it finishes; resolves to its result. */
async function pollJob<T>(jobId: string, onProgress?: (p: JobProgress) => void): Promise<T> {
  for (;;) {
    const job = await request<Job>(`/api/jobs/${jobId}`);
    if (job.status === "done") return job.result as T;
    if (job.status === "error") throw new ApiError(job.status_code ?? 500, job.detail ?? "Job failed");
    onProgress?.({ phase: job.phase, done: job.done, total: job.total });
    await new Promise((r) => setTimeout(r, 1000));
  }
}

interface ValidationIssue {
  loc?: (string | number)[];
  msg: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, `Please check your connection or try again shortly.`);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail))
        detail = (body.detail as ValidationIssue[])
          .map((d) => `${d.loc?.slice(1).join(".") ?? ""}: ${d.msg}`)
          .join("; ");
    } catch {
      // non-JSON error body; keep statusText
    }
    throw new ApiError(res.status, detail);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

/** Resolves to null instead of throwing on 404. */
async function optional<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export const api = {
  health: () => request<Health>("/api/health"),

  /** Starts a background crawl job and polls it until done. */
  crawl: async (url: string, opts: CrawlOptions, onProgress?: (p: JobProgress) => void) => {
    const { job_id } = await request<{ job_id: string }>("/api/crawl/jobs", {
      method: "POST",
      body: JSON.stringify({
        url,
        max_pages: opts.maxPages,
        max_tests_per_page: opts.maxTestsPerPage || null, // 0 = no limit
        replace_existing_ai: true,
        auth: opts.auth,
      }),
    });
    return pollJob<CrawlResponse>(job_id, onProgress);
  },

  /** Opens a visible browser on the server machine for the user to log in by hand. */
  startManualLogin: (loginUrl: string) =>
    request<{ job_id: string }>("/api/auth/manual-login", {
      method: "POST",
      body: JSON.stringify({ login_url: loginUrl }),
    }).then((r) => r.job_id),

  finishManualLogin: (jobId: string) =>
    request<{ ok: boolean }>(`/api/auth/manual-login/${jobId}/finish`, { method: "POST" }),

  waitForJob: pollJob,

  /** Refresh: wipes test cases, runs, the last crawl, the saved session and screenshots. */
  reset: () => request<{ ok: boolean }>("/api/reset", { method: "POST" }),

  latestCrawl: () => optional(request<CrawlSummary>("/api/crawl/latest")),

  listTestCases: () => request<TestCase[]>("/api/test-cases"),

  updateTestCase: (id: string, patch: Partial<TestCase>) =>
    request<TestCase>(`/api/test-cases/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),

  deleteTestCase: (id: string) =>
    request<void>(`/api/test-cases/${encodeURIComponent(id)}`, { method: "DELETE" }),

  runTests: async (testIds?: string[], onProgress?: (p: JobProgress) => void) => {
    const { job_id } = await request<{ job_id: string }>("/api/run-tests/jobs", {
      method: "POST",
      body: JSON.stringify(testIds ? { test_ids: testIds } : {}),
    });
    return pollJob<TestRun>(job_id, onProgress);
  },

  latestRun: () => optional(request<TestRun>("/api/runs/latest")),

  reportUrl: (format: ReportFormat, runId?: string) =>
    `${API_URL}/api/reports/download?format=${format}${runId ? `&run_id=${encodeURIComponent(runId)}` : ""}`,

  /** Backend returns paths like /static/screenshots/x.png */
  assetUrl: (path: string) => `${API_URL}${path}`,
};
