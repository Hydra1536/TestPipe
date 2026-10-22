"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { CrawlSummary } from "@/lib/types";

export function CrawlSummaryPanel({ crawl }: { crawl: CrawlSummary }) {
  const [open, setOpen] = useState(false);
  const a = crawl.auth;

  return (
    <div className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <span className="font-medium text-slate-800">{crawl.page_count} pages crawled</span>
        <a href={crawl.start_url} target="_blank" rel="noreferrer" className="truncate text-slate-500 hover:text-indigo-600">
          {crawl.start_url}
        </a>
        <span className="text-xs text-slate-400">{new Date(crawl.crawled_at).toLocaleString()}</span>
      </div>

      {a.attempted && (
        <div
          className={`rounded-md px-3 py-2 text-xs ${
            a.success ? "bg-emerald-50 text-emerald-800" : "bg-amber-50 text-amber-800"
          }`}
        >
          <span className="font-medium">
            {a.mode === "signup" ? "Auto sign-up" : "Login"}: {a.success ? "session established" : "not confirmed"}
          </span>
          {a.logged_in_as && <span> · as {a.logged_in_as}</span>}
          <p className="mt-0.5">{a.detail}</p>
        </div>
      )}

      <button onClick={() => setOpen((o) => !o)} className="text-xs font-medium text-indigo-600 hover:text-indigo-500">
        {open ? "− Hide pages" : "+ Show crawled pages"}
      </button>

      {open && (
        <ul className="grid gap-2 sm:grid-cols-2">
          {crawl.pages.map((p, i) => (
            <li key={`${p.url}-${i}`} className="flex items-center gap-3 rounded border border-slate-200 bg-white p-2">
              {p.screenshot && (
                <a href={api.assetUrl(p.screenshot)} target="_blank" rel="noreferrer" className="shrink-0">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={api.assetUrl(p.screenshot)}
                    alt={`Screenshot of ${p.url}`}
                    className="h-10 w-16 rounded border border-slate-200 object-cover object-top"
                  />
                </a>
              )}
              <div className="min-w-0 text-xs">
                <p className="truncate font-medium text-slate-800">
                  {p.title || "Untitled"}
                  {p.requires_auth && <span className="ml-1 text-amber-600" title="Behind login">🔒</span>}
                </p>
                <a href={p.url} target="_blank" rel="noreferrer" className="truncate text-slate-500 hover:text-indigo-600">
                  {p.url}
                </a>
                <p className="text-slate-400">{p.element_count} elements</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
