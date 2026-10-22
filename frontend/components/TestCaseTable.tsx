"use client";

import { Fragment, useState } from "react";
import { api } from "@/lib/api";
import type { TestCase, TestResult, TestStep } from "@/lib/types";
import { StatusBadge } from "./ui";

function formatStep(s: TestStep) {
  return [s.action, s.selector, s.value !== null ? JSON.stringify(s.value) : null].filter(Boolean).join("  ");
}

const RESULT_COLORS: Record<string, string> = {
  Pass: "text-emerald-700",
  Fail: "text-red-700",
  Error: "text-amber-700",
};

function Details({ tc, result }: { tc: TestCase; result?: TestResult }) {
  return (
    <div className="grid gap-4 bg-slate-50 px-4 py-4 text-sm md:grid-cols-2">
      <div className="space-y-3">
        {tc.description && <p className="text-slate-600">{tc.description}</p>}
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Action steps</h4>
          <ol className="mt-1 list-inside list-decimal space-y-0.5 text-slate-700">
            {tc.action_steps.map((s, i) => (
              <li key={i}>{s}</li>
            ))}
          </ol>
        </div>
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Expected outcome</h4>
          <p className="mt-1 text-slate-700">{tc.expected_outcome || "—"}</p>
          {result && (
            <p className={`mt-1 text-sm font-semibold ${RESULT_COLORS[result.status] ?? ""}`}>
              Result: {result.status}
              {result.error ? ` — ${result.error}` : ""}
            </p>
          )}
        </div>
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">
            Executable steps <span className="font-normal normal-case text-slate-400">on {tc.target_url}</span>
          </h4>
          {tc.steps.length ? (
            <pre className="mt-1 overflow-x-auto rounded border border-slate-200 bg-white p-2 font-mono text-xs text-slate-700">
              {tc.steps.map(formatStep).join("\n")}
            </pre>
          ) : (
            <p className="mt-1 text-amber-700">
              No executable steps. The AI couldn&apos;t translate this test, so it will report Error when run.
            </p>
          )}
        </div>
      </div>

      <div className="space-y-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Last run</h4>
        {result ? (
          <>
            <div className="flex items-center gap-3">
              <StatusBadge status={result.status} />
              <span className="text-xs text-slate-500">{result.duration_ms} ms</span>
            </div>
            {result.error && <p className="break-words rounded bg-red-50 p-2 text-xs text-red-800">{result.error}</p>}
            <pre className="max-h-56 overflow-auto rounded border border-slate-200 bg-slate-900 p-2 font-mono text-xs leading-relaxed text-slate-100">
              {result.logs.join("\n")}
            </pre>
            {result.screenshot && (
              <a href={api.assetUrl(result.screenshot)} target="_blank" rel="noreferrer" className="block">
                {/* plain <img>: next/image blocks local-IP sources by default */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={api.assetUrl(result.screenshot)}
                  alt={`Screenshot when ${tc.id} failed`}
                  className="max-h-48 rounded border border-slate-200"
                />
              </a>
            )}
          </>
        ) : (
          <p className="text-slate-400">This test hasn&apos;t run yet.</p>
        )}
      </div>
    </div>
  );
}

export function TestCaseTable({
  testCases,
  results,
  running,
  onToggle,
  onDelete,
}: {
  testCases: TestCase[];
  results: Map<string, TestResult>;
  running: boolean;
  onToggle: (tc: TestCase) => void;
  onDelete: (tc: TestCase) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleRow = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (!testCases.length)
    return (
      <div className="px-6 py-16 text-center">
        <p className="font-medium text-slate-700">No test cases yet</p>
        <p className="mt-1 text-sm text-slate-500">
          Enter a URL above to generate tests with AI, or add one manually.
        </p>
      </div>
    );

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-slate-200 bg-slate-50 text-xs uppercase tracking-wide text-slate-500">
          <tr>
            <th className="w-10 px-4 py-2.5 font-medium">Run</th>
            <th className="px-3 py-2.5 font-medium">ID</th>
            <th className="px-3 py-2.5 font-medium">Title</th>
            <th className="px-3 py-2.5 font-medium">Page</th>
            <th className="px-3 py-2.5 font-medium">Type</th>
            <th className="px-3 py-2.5 font-medium">Status</th>
            <th className="px-3 py-2.5 font-medium">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {testCases.map((tc) => {
            const result = results.get(tc.id);
            const isOpen = expanded.has(tc.id);
            return (
              <Fragment key={tc.id}>
                <tr className={`${tc.enabled ? "" : "opacity-50"} hover:bg-slate-50/60`}>
                  <td className="px-4 py-2.5">
                    <input
                      type="checkbox"
                      checked={tc.enabled}
                      disabled={running}
                      onChange={() => onToggle(tc)}
                      aria-label={`Include ${tc.id} in runs`}
                      className="h-4 w-4 rounded border-slate-300 accent-indigo-600"
                    />
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 font-mono text-xs text-slate-500">{tc.id}</td>
                  <td className="px-3 py-2.5">
                    <button onClick={() => toggleRow(tc.id)} className="text-left font-medium text-slate-800 hover:text-indigo-600">
                      {tc.title}
                    </button>
                    <p className="line-clamp-1 text-xs text-slate-500">{tc.expected_outcome}</p>
                  </td>
                  <td className="max-w-[12rem] px-3 py-2.5">
                    {tc.page_url ? (
                      <a
                        href={tc.page_url}
                        target="_blank"
                        rel="noreferrer"
                        title={tc.page_url}
                        className="block truncate text-xs text-slate-600 hover:text-indigo-600"
                      >
                        {tc.page_title || tc.page_url}
                        {tc.requires_auth && <span className="ml-1 text-amber-600" title="Behind login">🔒</span>}
                      </a>
                    ) : (
                      <span className="text-xs text-slate-400">—</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-slate-600">{tc.test_type}</td>
                  <td className="whitespace-nowrap px-3 py-2.5">
                    <StatusBadge status={running && tc.enabled ? "Running" : (result?.status ?? null)} />
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-right">
                    <button
                      onClick={() => toggleRow(tc.id)}
                      className="rounded px-2 py-1 text-xs text-slate-500 hover:bg-slate-100"
                      aria-expanded={isOpen}
                    >
                      {isOpen ? "Hide" : "Details"}
                    </button>
                    <button
                      onClick={() => onDelete(tc)}
                      disabled={running}
                      className="rounded px-2 py-1 text-xs text-red-600 hover:bg-red-50 disabled:text-red-300"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={7} className="p-0">
                      <Details tc={tc} result={result} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
