import type { TestRun } from "@/lib/types";

export function SummaryCards({ testCount, run }: { testCount: number; run: TestRun | null }) {
  const cards = [
    { label: "Total Tests", value: testCount, className: "text-slate-900" },
    { label: "Passed", value: run?.passed, className: "text-emerald-600" },
    { label: "Failed", value: run?.failed, className: "text-red-600" },
    { label: "Errors", value: run?.errors, className: "text-amber-600" },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {cards.map((c) => (
        <div key={c.label} className="rounded-lg border border-slate-200 bg-white px-4 py-3 shadow-sm">
          <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{c.label}</p>
          <p className={`mt-1 text-2xl font-semibold tabular-nums ${c.className}`}>{c.value ?? "–"}</p>
        </div>
      ))}
    </div>
  );
}
