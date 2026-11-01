"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Finding = {
  title: string;
  severity: string;
  points: number;
  source_url?: string;
  evidence_urls?: string[];
  excerpt?: string;
  retrieved_at?: string;
  tinyfish_tool?: string;
  run_id?: string;
};

type RunLog = {
  id: string;
  tool: string;
  input_summary: string;
  target_url?: string;
  run_id?: string;
  status: string;
  started_at: string;
  duration_ms?: number;
  raw_output_json?: unknown;
  is_cached: boolean;
};

type EvidenceItem = {
  title?: string;
  url?: string;
  snippet?: string;
  excerpt?: string;
  publisher?: string;
  site_name?: string;
  date?: string;
  classification?: {
    relevant: boolean;
    negative: boolean;
    issue_category: string | null;
    verified: boolean;
  };
};

type Audit = {
  id: string;
  status: string;
  project_name?: string;
  builder?: string;
  city?: string;
  state?: string;
  claims: Record<string, string | null>;
  rera_record?: Record<string, unknown> | null;
  findings: Finding[];
  news: EvidenceItem[];
  residents: EvidenceItem[];
  builder_projects: Array<{
    project_name?: string;
    rera_id?: string;
    status?: string;
    extensions_count?: number;
    complaints_count?: number;
    project_url?: string;
    run_id?: string;
  }>;
  score: {
    score: number | null;
    risk_band: string;
    confidence: string;
    deductions: Finding[];
  };
  progress: Array<{ stage: string; status: string; message: string }>;
  disclaimer: string;
};

function display(value: unknown) {
  return value == null || value === "" ? "Not retrieved" : String(value);
}

function ReportPanel({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="report-panel">
      <div className="panel-heading">
        <div><h2>{title}</h2>{subtitle && <p>{subtitle}</p>}</div>
      </div>
      {children}
    </section>
  );
}

export default function ReportPage() {
  const params = useParams<{ id: string }>();
  const [audit, setAudit] = useState<Audit | null>(null);
  const [runs, setRuns] = useState<RunLog[]>([]);
  const [error, setError] = useState("");
  const [watchMessage, setWatchMessage] = useState("");
  const [shareMessage, setShareMessage] = useState("");

  const refresh = useCallback(async () => {
    const response = await fetch(`${API}/api/audits/${params.id}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Could not load this audit.");
    setAudit(await response.json());
    const runResponse = await fetch(`${API}/api/audits/${params.id}/runs`, {
      cache: "no-store",
    });
    if (runResponse.ok) setRuns(await runResponse.json());
  }, [params.id]);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        await refresh();
      } catch {
        if (active) setError("Could not reach the TrueEstate API. Check that the backend is running.");
      }
    };
    void load();
    const events = new EventSource(`${API}/api/audits/${params.id}/events`);
    events.onmessage = () => { void load(); };
    const interval = window.setInterval(() => { void load(); }, 5000);
    return () => {
      active = false;
      events.close();
      window.clearInterval(interval);
    };
  }, [params.id, refresh]);

  async function createWatch() {
    setWatchMessage("");
    try {
      const response = await fetch(`${API}/api/watches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audit_id: params.id }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Monitor could not be created.");
      setWatchMessage(`Monitoring is active. TinyFish monitor ID: ${body.monitor_id}`);
    } catch (reason) {
      setWatchMessage(reason instanceof Error ? reason.message : "Monitor could not be created.");
    }
  }

  async function selectCandidate(candidate: Record<string, unknown>) {
    const response = await fetch(`${API}/api/audits/${params.id}/select-candidate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidate }),
    });
    if (response.ok) await refresh();
    else {
      const body = await response.json();
      setError(typeof body.detail === "string" ? body.detail : "Could not select this RERA project.");
    }
  }

  async function shareReport() {
    try {
      await navigator.clipboard.writeText(window.location.href);
      setShareMessage("Read-only report link copied.");
    } catch {
      setShareMessage("Could not copy the report link. Copy it from your browser address bar.");
    }
  }

  if (error && !audit) return <main className="report-page"><p className="form-error">{error}</p><Link href="/">← Back to TrueEstate</Link></main>;
  if (!audit) return <main className="report-page"><div className="loading-mark">T</div><p>Starting your property audit…</p></main>;

  const record = audit.rera_record ?? {};
  const candidates = Array.isArray(record.candidates)
    ? record.candidates as Array<Record<string, unknown>>
    : [];
  const isRunning = ["queued", "running"].includes(audit.status);
  const score = audit.score?.score;

  return (
    <main className="report-page">
      <header className="topbar report-topbar">
        <Link className="brand" href="/"><span className="brand-mark">T</span><span>TrueEstate</span></Link>
        <nav><Link href="/watchlist">Watchlist <span aria-hidden="true">↗</span></Link></nav>
      </header>
      <div className="report-title">
        <div>
          <div className="eyebrow"><span className={isRunning ? "live-dot pulse" : "live-dot"} /> PROPERTY AUDIT <span className="title-slash">/</span> {audit.status.replace("_", " ").toUpperCase()}</div>
          <h1>{audit.project_name || "Your property report"}</h1>
          <p>{[audit.builder, audit.city, audit.state].filter(Boolean).join(" · ") || "Project details are being extracted from the listing."}</p>
        </div>
        <div className="report-actions">
          <button className="outline-button" type="button" onClick={() => window.print()}>Print report</button>
          <button className="outline-button" type="button" onClick={() => void shareReport()}>Share report ↗</button>
          {typeof record.project_url === "string" && (
            <button className="outline-button" type="button" onClick={createWatch}>＋ Watch this project</button>
          )}
        </div>
      </div>
      {watchMessage && <div className="inline-notice" role="status">{watchMessage}</div>}
      {shareMessage && <div className="inline-notice" role="status">{shareMessage}</div>}
      {error && <div className="inline-notice error-notice" role="alert">{error}</div>}

      {isRunning && (
        <section className="progress-card">
          <div className="progress-head"><span className="live-dot pulse" /> AUDIT IN PROGRESS <span className="muted">{audit.status}</span></div>
          <div className="progress-list">
            {["extract", "rera", "news", "residents", "classification", "audit"].map((stage) => {
              const latest = [...audit.progress].reverse().find((item) => item.stage === stage);
              const label = stage === "rera" ? "Official RERA record" : stage === "audit" ? "Trust score" : stage === "residents" ? "Resident evidence" : stage === "news" ? "News coverage" : stage === "classification" ? "Evidence checks" : "Listing claims";
              return <div key={stage} className="progress-item"><span className={`progress-icon ${latest?.status ?? ""}`}>{latest?.status === "completed" ? "✓" : latest?.status === "failed" ? "!" : latest?.status === "skipped" ? "–" : "·"}</span><div><b>{label}</b><p>{latest?.message ?? "Waiting to start…"}</p></div></div>;
            })}
          </div>
        </section>
      )}

      {(audit.status === "needs_input" || audit.status === "unsupported") && (
        <div className="inline-notice">{audit.status === "unsupported" ? `Live RERA audits currently support Karnataka only. No official records were queried for ${audit.state || "this state"}.` : "Listing details could not be confirmed automatically. Start a manual audit from the home page to enter project, builder and city."}</div>
      )}

      <div className="report-grid">
        <section className={`score-card risk-${(audit.score?.risk_band ?? "unknown").toLowerCase()}`}>
          <div className="card-kicker">TRUEESTATE TRUST SCORE</div>
          <div className="score-number">{score ?? "—"}<span>{score == null ? "" : "/100"}</span></div>
          <div className="risk-label">{audit.score?.risk_band ?? "Pending"} risk</div>
          <div className="confidence-line">Confidence: <b>{audit.score?.confidence ?? "Low"}</b></div>
          <div className="score-track"><span style={{ width: `${score ?? 0}%` }} /></div>
          <p>Rule-based score from the evidence retrieved so far. It is not a verdict or legal advice.</p>
        </section>

        <ReportPanel title="Listing claims" subtitle="What was provided or extracted">
          <dl className="detail-list">
            <div><dt>Project</dt><dd>{display(audit.claims.project_name)}</dd></div>
            <div><dt>Builder</dt><dd>{display(audit.claims.builder)}</dd></div>
            <div><dt>Possession</dt><dd>{display(audit.claims.possession_date)}</dd></div>
            <div><dt>RERA ID</dt><dd>{display(audit.claims.rera_id)}</dd></div>
            <div><dt>Carpet area</dt><dd>{display(audit.claims.carpet_area)}</dd></div>
            <div><dt>Price</dt><dd>{display(audit.claims.price)}</dd></div>
          </dl>
        </ReportPanel>
      </div>

      <ReportPanel title="Official RERA record" subtitle="Karnataka RERA · Live retrieval status shown below">
        {record.found === true ? (
          <dl className="detail-list record-list">
            <div><dt>Project</dt><dd>{display(record.project_name)}</dd></div>
            <div><dt>Promoter</dt><dd>{display(record.promoter)}</dd></div>
            <div><dt>Registration</dt><dd>{display(record.status)}</dd></div>
            <div><dt>RERA ID</dt><dd>{display(record.rera_id)}</dd></div>
            <div><dt>Original completion</dt><dd>{display(record.original_completion_date)}</dd></div>
            <div><dt>Revised completion</dt><dd>{display(record.revised_completion_date)}</dd></div>
            <div><dt>Extensions</dt><dd>{display(record.extensions_count)}</dd></div>
            <div><dt>Complaints</dt><dd>{display(record.complaints_count)}</dd></div>
          </dl>
        ) : <p className="empty-state">{record.source_error ? String(record.source_error) : record.ambiguous ? "Several plausible records were returned. Select the matching project below to calculate a score." : record.found === false ? "No matching RERA record was returned." : isRunning ? "Waiting for the RERA portal result…" : "RERA record not retrieved. Check the Agent Console for the reason."}</p>}
        {typeof record.project_url === "string" && <a className="source-link" href={record.project_url} target="_blank" rel="noreferrer">Open official project record ↗</a>}
        {record.ambiguous === true && candidates.length > 1 && (
          <div className="candidate-list"><h3>Choose the matching project</h3>{candidates.map((candidate, index) => <button key={`${String(candidate.rera_id)}-${index}`} disabled={typeof candidate.project_url !== "string"} onClick={() => void selectCandidate(candidate)} className="candidate-row"><span><b>{display(candidate.project_name)}</b><small>{display(candidate.promoter)} · {display(candidate.rera_id)}</small></span><span>{typeof candidate.project_url === "string" ? "Use this →" : "No page link"}</span></button>)}</div>
        )}
      </ReportPanel>

      <ReportPanel title="Builder track record" subtitle="Other Karnataka RERA projects checked in parallel">
        {audit.builder_projects?.length ? (
          <div className="builder-table-wrap">
            <table className="builder-table">
              <thead><tr><th>Project</th><th>RERA ID</th><th>Status</th><th>Extensions</th><th>Complaints</th><th>Source</th></tr></thead>
              <tbody>{audit.builder_projects.map((project, index) => (
                <tr key={`${project.rera_id || project.project_name}-${index}`}>
                  <td>{project.project_name || "Not retrieved"}</td>
                  <td>{project.rera_id || "Not retrieved"}</td>
                  <td>{project.status || "Not retrieved"}</td>
                  <td>{project.extensions_count ?? "Not retrieved"}</td>
                  <td>{project.complaints_count ?? "Not retrieved"}</td>
                  <td>{project.project_url ? <a href={project.project_url} target="_blank" rel="noreferrer">RERA ↗</a> : "Not retrieved"}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p className="empty-state">{isRunning ? "Checking up to five other registered projects by this builder…" : "No additional builder projects were verified."}</p>}
      </ReportPanel>

      <ReportPanel title="Findings" subtitle="Deductions are deterministic and linked to retrieved evidence">
        {audit.score?.deductions?.length ? (
          <div className="finding-list">
            {audit.score.deductions.map((finding, index) => (
              <article className="finding" key={`${finding.title}-${index}`}>
                <span className={`severity severity-${finding.severity}`}>{finding.severity}</span>
                <div className="finding-copy"><h3>{finding.title}</h3><p>{finding.excerpt}</p>{(finding.evidence_urls?.length ? finding.evidence_urls : finding.source_url ? [finding.source_url] : []).map((url, evidenceIndex) => <a className="source-link evidence-source-link" href={url} target="_blank" rel="noreferrer" key={`${url}-${evidenceIndex}`}>Evidence source {evidenceIndex + 1} ↗</a>)}<p className="finding-meta">Retrieved {finding.retrieved_at ? new Date(finding.retrieved_at).toLocaleString() : "time not recorded"} · {finding.tinyfish_tool || "Source"} · Trace/run ID {finding.run_id || "not returned"}</p></div>
                <b className="deduction">−{finding.points}</b>
              </article>
            ))}
          </div>
        ) : <p className="empty-state">No score deductions were established from retrieved evidence. Missing data is not proof of low risk.</p>}
      </ReportPanel>

      <div className="report-grid evidence-grid">
        <ReportPanel title="Local news" subtitle="Recent public coverage">
          <EvidenceList items={audit.news} empty={isRunning ? "Searching recent news…" : "No news results retrieved."} />
        </ReportPanel>
        <ReportPanel title="Resident evidence" subtitle="Public community mentions · unverified unless independently confirmed">
          <EvidenceList items={audit.residents} empty={isRunning ? "Searching community sources…" : "No community results retrieved."} />
        </ReportPanel>
      </div>

      <ReportPanel title="Agent Console" subtitle="TinyFish activity and raw outputs · LIVE runs are never presented as cached">
        {runs.length ? (
          <div className="run-list">
            {runs.map((run) => (
              <details className="run-card" key={run.id}>
                <summary>
                  <span className={`run-state ${run.status.toLowerCase()}`}>{run.status}</span>
                  <b>{run.tool}</b>
                  <span className="run-summary">{run.input_summary}</span>
                  <span className="live-badge">{run.is_cached ? "CACHED" : "LIVE"}</span>
                </summary>
                <div className="run-details">
                  <p><b>Target:</b> {run.target_url ?? "Not applicable"}</p>
                  <p><b>{run.run_id ? "TinyFish run ID" : "TrueEstate trace ID"}:</b> {run.run_id ?? run.id}</p>
                  <p><b>Started:</b> {new Date(run.started_at).toLocaleString()} · <b>Duration:</b> {run.duration_ms == null ? "—" : `${run.duration_ms} ms`}</p>
                  <pre>{JSON.stringify(run.raw_output_json, null, 2)}</pre>
                </div>
              </details>
            ))}
          </div>
        ) : <p className="empty-state">No TinyFish runs recorded yet. Failed attempts also appear here.</p>}
      </ReportPanel>

      <footer className="report-footer"><Link href="/">← New audit</Link><span>{audit.disclaimer}</span><span>Audit ID: {audit.id}</span></footer>
    </main>
  );
}

function EvidenceList({ items, empty }: { items: EvidenceItem[]; empty: string }) {
  if (!items.length) return <p className="empty-state">{empty}</p>;
  return <div className="evidence-list">{items.map((item, index) => (
    <article className="evidence-item" key={`${item.url}-${index}`}>
      <h3>{item.title || "Public source"}</h3>
      <p>{item.excerpt || item.snippet || "No readable excerpt returned."}</p>
      {item.classification && <p className="classification-label">{item.classification.verified ? item.classification.relevant ? item.classification.issue_category ? `Reported topic: ${item.classification.issue_category.replaceAll("_", " ")}` : item.classification.negative ? "Relevant negative coverage" : "Relevant source" : "Not project-relevant" : "AI quote not verified; excluded from scoring"}</p>}
      <div className="evidence-meta"><span>{item.publisher || item.site_name || item.date || "Public web result"}</span>{item.url && <a href={item.url} target="_blank" rel="noreferrer">Source ↗</a>}</div>
    </article>
  ))}</div>;
}
