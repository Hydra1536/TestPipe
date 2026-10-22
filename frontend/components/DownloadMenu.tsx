"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { ReportFormat } from "@/lib/types";

const FORMATS: { format: ReportFormat; label: string; hint: string }[] = [
  { format: "pdf", label: "PDF report", hint: "Print / share" },
  { format: "html", label: "HTML report", hint: "Readable, shareable" },
  { format: "csv", label: "CSV", hint: "For spreadsheets" },
  { format: "json", label: "JSON", hint: "Run + test cases" },
];

export function DownloadMenu({ runId }: { runId?: string }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const disabled = !runId;

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        disabled={disabled}
        title={disabled ? "Run tests first" : undefined}
        aria-haspopup="menu"
        aria-expanded={open}
        className="inline-flex items-center gap-2 rounded-md bg-white px-3.5 py-2 text-sm font-medium text-slate-700 ring-1 ring-inset ring-slate-300 hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
      >
        Download Report
        <span aria-hidden className="text-xs">▾</span>
      </button>
      {open && (
        <div role="menu" className="absolute right-0 z-10 mt-1 w-52 overflow-hidden rounded-md border border-slate-200 bg-white py-1 shadow-lg">
          {FORMATS.map((f) => (
            <a
              key={f.format}
              role="menuitem"
              href={api.reportUrl(f.format, runId)}
              onClick={() => setOpen(false)}
              className="block px-3 py-2 text-sm hover:bg-slate-50"
            >
              <span className="font-medium text-slate-800">{f.label}</span>
              <span className="block text-xs text-slate-500">{f.hint}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}
