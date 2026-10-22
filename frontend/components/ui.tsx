import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { JobProgress, TestStatus } from "@/lib/types";

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none" aria-hidden>
      <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" className="opacity-25" />
      <path d="M22 12a10 10 0 0 0-10-10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-indigo-600 text-white hover:bg-indigo-500 disabled:bg-indigo-300",
  secondary:
    "bg-white text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:text-slate-400",
  ghost: "text-slate-600 hover:bg-slate-100 disabled:text-slate-300",
  danger: "text-red-600 hover:bg-red-50 disabled:text-red-300",
};

export function Button({
  variant = "secondary",
  loading = false,
  className = "",
  children,
  disabled,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; loading?: boolean }) {
  return (
    <button
      {...props}
      disabled={disabled || loading}
      className={`inline-flex items-center justify-center gap-2 rounded-md px-3.5 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed ${VARIANTS[variant]} ${className}`}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}

const STATUS_STYLES: Record<TestStatus, string> = {
  Pass: "bg-emerald-50 text-emerald-700 ring-emerald-600/20",
  Fail: "bg-red-50 text-red-700 ring-red-600/20",
  Error: "bg-amber-50 text-amber-800 ring-amber-600/20",
};

export function StatusBadge({ status }: { status: TestStatus | "Running" | null }) {
  if (status === "Running")
    return (
      <span className="inline-flex items-center gap-1.5 rounded-full bg-indigo-50 px-2 py-0.5 text-xs font-medium text-indigo-700 ring-1 ring-inset ring-indigo-600/20">
        <Spinner className="h-3 w-3" /> Running
      </span>
    );
  if (!status) return <span className="text-xs text-slate-400">Not run</span>;
  return (
    <span className={`inline-flex rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLES[status]}`}>
      {status === "Pass" ? "Passed" : status === "Fail" ? "Failed" : "Error"}
    </span>
  );
}

const PHASE_LABELS: Record<string, string> = {
  crawling: "Crawling pages",
  generating: "Generating tests",
  running: "Running tests",
  waiting_for_login: "Waiting for you to log in",
};

/** Job progress. Unknown total (e.g. crawling) shows an indeterminate bar. */
export function ProgressBar({ progress, elapsed }: { progress: JobProgress | null; elapsed: number }) {
  const { phase, done = 0, total = null } = progress ?? {};
  const pct = total ? Math.round((done / total) * 100) : null;
  const count = total ? `${done}/${total}` : phase === "crawling" ? `${done} found` : "";
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-slate-500">
        <span>
          {PHASE_LABELS[phase ?? ""] ?? "Starting"}… {count}
        </span>
        <span>
          {pct !== null && `${pct}% · `}
          {elapsed}s
        </span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-slate-100"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct ?? undefined}
      >
        <div
          className={`h-full rounded-full bg-indigo-500 transition-all ${pct === null ? "w-1/3 animate-pulse" : ""}`}
          style={pct === null ? undefined : { width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`rounded-lg border border-slate-200 bg-white shadow-sm ${className}`}>{children}</div>;
}

export function Alert({ children, onClose }: { children: ReactNode; onClose?: () => void }) {
  return (
    <div role="alert" className="flex items-start justify-between gap-4 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <p className="break-words">{children}</p>
      {onClose && (
        <button onClick={onClose} className="shrink-0 text-red-500 hover:text-red-700" aria-label="Dismiss">
          ✕
        </button>
      )}
    </div>
  );
}
