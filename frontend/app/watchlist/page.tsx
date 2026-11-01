"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Watch = {
  id: string;
  audit_id: string;
  monitor_id?: string;
  type: "fetch" | "search";
  project_name?: string;
  builder?: string;
  target: string;
  cron: string;
  last_checked_at?: string;
  last_change_json?: unknown;
};

export default function WatchlistPage() {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API}/api/watches`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not load the watchlist.");
        setWatches(await response.json());
      })
      .catch(() => setError("Could not reach the TrueEstate API."));
  }, []);

  return (
    <main className="report-page">
      <header className="topbar report-topbar">
        <Link className="brand" href="/"><span className="brand-mark">T</span><span>TrueEstate</span></Link>
        <nav><Link href="/">New audit <span aria-hidden="true">↗</span></Link></nav>
      </header>
      <div className="report-title">
        <div><div className="eyebrow">ONGOING DUE DILIGENCE</div><h1>Your watchlist</h1><p>Public RERA project pages monitored by TinyFish.</p></div>
      </div>
      {error && <div className="inline-notice error-notice">{error}</div>}
      {watches.length ? (
        <section className="watch-list">
          {watches.map((watch) => (
            <article className="watch-card" key={watch.id}>
              <div><span className="live-badge">{watch.type === "search" ? "NEWS MONITOR" : "RERA MONITOR"}</span><h2>{watch.project_name || "Property project"}</h2><p>{watch.builder || "Builder not provided"}</p></div>
              <dl className="detail-list">
                <div><dt>Schedule</dt><dd>{watch.cron}</dd></div>
                <div><dt>Last checked</dt><dd>{watch.last_checked_at ? new Date(watch.last_checked_at).toLocaleString() : "Waiting for first check"}</dd></div>
                <div><dt>TinyFish monitor</dt><dd>{watch.monitor_id || "Not assigned"}</dd></div>
              </dl>
              {watch.last_change_json != null && <details><summary>Latest change received</summary><pre>{JSON.stringify(watch.last_change_json, null, 2)}</pre></details>}
              {watch.type === "fetch" && <a className="source-link" href={watch.target} target="_blank" rel="noreferrer">Official page ↗</a>}
              {watch.type === "search" && <p className="watch-query"><b>Search topic:</b> {watch.target}</p>}
              <Link className="watch-report-link" href={`/report/${watch.audit_id}`}>Open audit report →</Link>
            </article>
          ))}
        </section>
      ) : (
        <section className="report-panel watch-empty"><div className="empty-graphic">↗</div><h2>No monitored projects yet</h2><p>Run a property audit, then choose “Watch this project” to monitor its public RERA page.</p><Link className="primary-button inline-button" href="/">Start an audit <span>→</span></Link></section>
      )}
      <footer className="report-footer"><Link href="/">← TrueEstate home</Link><span>Alerts depend on TinyFish monitor and webhook availability.</span></footer>
    </main>
  );
}
