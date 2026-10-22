// Mirrors backend/app/models.py

export interface TestStep {
  action: string;
  selector: string | null;
  value: string | null;
}

export interface TestCase {
  id: string;
  title: string;
  description: string;
  action_steps: string[];
  expected_outcome: string;
  test_type: string;
  source: "ai" | "manual";
  target_url: string;
  page_url: string;
  page_title: string;
  requires_auth: boolean;
  steps: TestStep[];
  enabled: boolean;
  created_at: string;
}

export type TestStatus = "Pass" | "Fail" | "Error";

export interface TestResult {
  test_id: string;
  title: string;
  test_type: string;
  source: string;
  page_url: string;
  page_title: string;
  status: TestStatus;
  duration_ms: number;
  logs: string[];
  error: string | null;
  screenshot: string | null;
}

export interface TestRun {
  run_id: string;
  started_at: string;
  finished_at: string;
  total: number;
  passed: number;
  failed: number;
  errors: number;
  results: TestResult[];
}

// ---- Crawl ----------------------------------------------------------------

export type AuthMode = "none" | "signup" | "login" | "session";

export interface JobProgress {
  phase: "crawling" | "generating" | "running" | "waiting_for_login" | null;
  done: number;
  total: number | null;
}

export interface CrawlAuthInput {
  mode: AuthMode;
  login_url?: string;
  email?: string;
  username?: string;
  password?: string;
}

export interface CrawlOptions {
  maxPages: number;
  maxTestsPerPage: number;
  auth: CrawlAuthInput;
}

export interface AuthReport {
  attempted: boolean;
  mode: string;
  success: boolean;
  detail: string;
  logged_in_as: string | null;
  logs: string[];
}

export interface CrawlPage {
  url: string;
  title: string;
  screenshot: string | null;
  element_count: number;
  requires_auth: boolean;
}

export interface CrawlSummary {
  start_url: string;
  crawled_at: string;
  page_count: number;
  auth: AuthReport;
  pages: CrawlPage[];
}

export interface CrawlResponse {
  crawl: CrawlSummary;
  generated_count: number;
  test_cases: TestCase[];
}

export interface Health {
  status: string;
  groq_configured: boolean;
  model: string;
}

export type ReportFormat = "json" | "csv" | "html" | "pdf";
