"use client";

import { useState, type FormEvent } from "react";
import type { AuthMode, CrawlOptions } from "@/lib/types";
import { api } from "@/lib/api";
import { Button } from "./ui";

const fieldClass =
  "mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 disabled:bg-slate-50";

export function CrawlForm({
  loading,
  disabled,
  onSubmit,
}: {
  loading: boolean;
  disabled?: boolean;
  onSubmit: (url: string, opts: CrawlOptions) => void;
}) {
  const [url, setUrl] = useState("");
  const [maxPages, setMaxPages] = useState(50);
  const [maxTestsPerPage, setMaxTestsPerPage] = useState(0); // 0 = no limit
  const [showAuth, setShowAuth] = useState(false);
  const [authMode, setAuthMode] = useState<AuthMode>("none");
  const [loginUrl, setLoginUrl] = useState("");
  const [loginJob, setLoginJob] = useState<string | null>(null);
  const [loginStatus, setLoginStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  async function openLoginBrowser() {
    let value = loginUrl.trim() || url.trim();
    if (!value) return;
    if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
    setLoginStatus(null);
    try {
      const id = await api.startManualLogin(value);
      setLoginJob(id);
      await api.waitForJob(id);
      setLoginStatus({ ok: true, msg: "Session saved. Now click Generate Test Cases." });
    } catch (e) {
      setLoginStatus({ ok: false, msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setLoginJob(null);
    }
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    let value = url.trim();
    if (!value) return;
    if (!/^https?:\/\//i.test(value)) value = `https://${value}`;
    setUrl(value);
    onSubmit(value, {
      maxPages,
      maxTestsPerPage,
      auth: {
        mode: authMode,
        login_url: loginUrl.trim() || undefined,
      },
    });
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-3">
      <div className="flex flex-col gap-3 sm:flex-row">
        <label className="sr-only" htmlFor="url">
          Website URL
        </label>
        <input
          id="url"
          type="text"
          inputMode="url"
          placeholder="https://your-website.com"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          disabled={loading}
          className="min-w-0 flex-1 rounded-md border border-slate-300 px-3 py-2 text-sm shadow-sm placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500/20 disabled:bg-slate-50"
        />
        <Button type="submit" variant="primary" loading={loading} disabled={disabled || !url.trim()}>
          {loading ? "Crawling & generating…" : "Generate Test Cases"}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-4 text-sm text-slate-600">
        <label className="flex items-center gap-2">
          Max pages
          <input
            type="number"
            min={1}
            max={500}
            value={maxPages}
            onChange={(e) => setMaxPages(Math.min(500, Math.max(1, Number(e.target.value) || 1)))}
            disabled={loading}
            className="w-20 rounded-md border border-slate-300 px-2 py-1 shadow-sm focus:border-indigo-500 focus:outline-none disabled:bg-slate-50"
          />
        </label>
        <label className="flex items-center gap-2">
          Max tests / page
          <select
            value={maxTestsPerPage}
            onChange={(e) => setMaxTestsPerPage(Number(e.target.value))}
            disabled={loading}
            className="rounded-md border border-slate-300 bg-white px-2 py-1 shadow-sm focus:border-indigo-500 focus:outline-none disabled:bg-slate-50"
          >
            <option value={0}>No limit</option>
            {[8, 12, 20, 30, 40].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          onClick={() => setShowAuth((s) => !s)}
          disabled={loading}
          className="text-indigo-600 hover:text-indigo-500"
        >
          {showAuth ? "− Hide login options" : "+ Test pages behind a login"}
        </button>
      </div>

      {showAuth && (
        <div className="space-y-3 rounded-md border border-slate-200 bg-slate-50 p-4">
          <p className="text-xs text-slate-500">
            Only use this on sites you own or are authorized to test.
          </p>
          <div className="flex flex-wrap gap-4 text-sm">
            {(
              [
                ["none", "Public pages only"],
                ["session", "Log in myself (Google, 2FA…)"],
              ] as [AuthMode, string][]
            ).map(([mode, label]) => (
              <label key={mode} className="flex items-center gap-1.5">
                <input
                  type="radio"
                  name="authMode"
                  checked={authMode === mode}
                  onChange={() => setAuthMode(mode)}
                  disabled={loading}
                  className="accent-indigo-600"
                />
                {label}
              </label>
            ))}
          </div>

          {authMode === "session" && (
            <div className="space-y-2 text-sm">
              <p className="text-xs text-slate-500">
                A real browser window opens on the machine running the backend. Log in there any way you like
                (Sign in with Google, 2FA, CAPTCHA), then click Done. Only the session cookies are saved, never
                your password.
              </p>
              <input
                type="url"
                value={loginUrl}
                onChange={(e) => setLoginUrl(e.target.value)}
                disabled={loading || !!loginJob}
                placeholder="Login page URL (defaults to the start URL)"
                className={fieldClass}
              />
              <div className="flex flex-wrap items-center gap-2">
                {loginJob ? (
                  <>
                    <span className="text-xs text-slate-600">Browser opened. Log in there, then:</span>
                    <Button type="button" variant="primary" onClick={() => api.finishManualLogin(loginJob)}>
                      Done, I&apos;m logged in
                    </Button>
                  </>
                ) : (
                  <Button type="button" onClick={openLoginBrowser} disabled={loading || !(loginUrl.trim() || url.trim())}>
                    Open browser to log in
                  </Button>
                )}
                {loginStatus && (
                  <span className={`text-xs ${loginStatus.ok ? "text-emerald-700" : "text-red-700"}`}>
                    {loginStatus.ok ? "✓ " : ""}
                    {loginStatus.msg}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </form>
  );
}
