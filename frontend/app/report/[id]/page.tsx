"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Finding = {
  type?: string;
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
  audit_id: string;
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
  source_type?: string;
  source_scope?: string;
  retrieved_at?: string;
  tinyfish_tool?: string;
  trace_id?: string;
  classification?: {
    relevant: boolean;
    negative: boolean;
    issue_category: string | null;
    topic?: string | null;
    summary?: string | null;
    verified: boolean;
    excerpt?: string;
  };
};

type Audit = {
  id: string;
  status: string;
  project_name?: string;
  builder?: string;
  locality_name?: string;
  city?: string;
  state?: string;
  claims: Record<string, string | null>;
  rera_record?: Record<string, unknown> | null;
  findings: Finding[];
  news: EvidenceItem[];
  residents: EvidenceItem[];
  locality: EvidenceItem[];
  research_status?: Record<string, {
    status: string;
    count?: number;
    message?: string;
  }>;
  builder_projects: Array<{
    project_name?: string;
    rera_id?: string;
    status?: string;
    original_completion_date?: string;
    revised_completion_date?: string;
    extensions_count?: number;
    complaints_count?: number;
    project_url?: string;
    run_id?: string;
    public_sources?: Array<{
      title: string;
      url: string;
      source_name?: string;
      snippet?: string;
    }>;
  }>;
  score: {
    score: number | null;
    risk_band: string;
    confidence: string;
    deductions: Finding[];
  };
  preliminary_assessment?: {
    score: number;
    basis: string;
    limitations: string;
  };
  progress: Array<{ stage: string; status: string; message: string }>;
  disclaimer: string;
};

type ManualAuditDetails = {
  project_name: string;
  builder: string;
  city: string;
  locality?: string;
};

function display(value: unknown) {
  return value == null || value === "" ? "Not retrieved" : String(value);
}

function ManualDetailsForm({
  audit,
  onSubmit,
}: {
  audit: Audit;
  onSubmit: (details: ManualAuditDetails) => void;
}) {
  const [details, setDetails] = useState<ManualAuditDetails>({
    project_name: audit.project_name || audit.claims.project_name || "",
    builder: audit.builder || audit.claims.builder || "",
    city: audit.city || audit.claims.city || "",
    locality: audit.locality_name || audit.claims.locality || "",
  });

  return (
    <section className="manual-details-card">
      <p>
        We extracted what we could from the listing, but need the project name,
        builder, and city to continue official RERA and locality research.
      </p>
      <form
        className="audit-form"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit({
            ...details,
            locality: details.locality?.trim() || undefined,
          });
        }}
      >
        <div className="form-grid">
          <label>
            PROJECT NAME
            <input
              required
              minLength={2}
              value={details.project_name}
              onChange={(event) =>
                setDetails({ ...details, project_name: event.target.value })
              }
            />
          </label>
          <label>
            BUILDER
            <input
              required
              minLength={2}
              value={details.builder}
              onChange={(event) =>
                setDetails({ ...details, builder: event.target.value })
              }
            />
          </label>
          <label>
            CITY
            <input
              required
              minLength={2}
              value={details.city}
              onChange={(event) =>
                setDetails({ ...details, city: event.target.value })
              }
            />
          </label>
          <label>
            LOCALITY <span className="optional">OPTIONAL</span>
            <input
              value={details.locality || ""}
              onChange={(event) =>
                setDetails({ ...details, locality: event.target.value })
              }
            />
          </label>
        </div>
        <button className="primary-button" type="submit">
          Continue this audit <span aria-hidden="true">→</span>
        </button>
      </form>
    </section>
  );
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
  return <AuditReport key={params.id} auditId={params.id} />;
}

function AuditReport({ auditId }: { auditId: string }) {
  const [audit, setAudit] = useState<Audit | null>(null);
  const [runs, setRuns] = useState<RunLog[]>([]);
  const [error, setError] = useState("");
  const [watchMessage, setWatchMessage] = useState("");
  const [shareMessage, setShareMessage] = useState("");
  const [retryMessage, setRetryMessage] = useState("");
  const requestSequence = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestSequence.current;
    let response: Response;
    let runResponse: Response;
    try {
      [response, runResponse] = await Promise.all([
        fetch(`${API}/api/audits/${auditId}`, { cache: "no-store" }),
        fetch(`${API}/api/audits/${auditId}/runs`, { cache: "no-store" }),
      ]);
    } catch (reason) {
      if (requestId !== requestSequence.current) return;
      throw reason;
    }
    if (requestId !== requestSequence.current) return;
    if (!response.ok) throw new Error("Could not load this audit.");
    if (!runResponse.ok) throw new Error("Could not load this audit's Agent Console logs.");
    const nextAudit = await response.json() as Audit;
    if (requestId !== requestSequence.current || nextAudit.id !== auditId) return;
    const auditRuns = await runResponse.json() as RunLog[];
    if (requestId !== requestSequence.current) return;
    setAudit(nextAudit);
    setRuns(auditRuns.filter((run) => run.audit_id === auditId));
  }, [auditId]);

  useEffect(() => {
    let active = true;
    requestSequence.current += 1;
    const load = async () => {
      try {
        await refresh();
      } catch {
        if (active) setError("Could not reach the TrueEstate API. Check that the backend is running.");
      }
    };
    void load();
    const events = new EventSource(`${API}/api/audits/${auditId}/events`);
    events.onmessage = () => { void load(); };
    const interval = window.setInterval(() => { void load(); }, 5000);
    return () => {
      active = false;
      requestSequence.current += 1;
      events.close();
      window.clearInterval(interval);
    };
  }, [auditId, refresh]);

  async function createWatch() {
    setWatchMessage("");
    try {
      const response = await fetch(`${API}/api/watches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ audit_id: auditId }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail ?? "Monitor could not be created.");
      setWatchMessage(`Monitoring is active. TinyFish monitor ID: ${body.monitor_id}`);
    } catch (reason) {
      setWatchMessage(reason instanceof Error ? reason.message : "Monitor could not be created.");
    }
  }

  async function selectCandidate(candidate: Record<string, unknown>) {
    const response = await fetch(`${API}/api/audits/${auditId}/select-candidate`, {
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

  async function retryResearch(details?: ManualAuditDetails) {
    setRetryMessage("");
    try {
      const response = await fetch(`${API}/api/audits/${auditId}/retry`, {
        method: "POST",
        headers: details ? { "Content-Type": "application/json" } : undefined,
        body: details ? JSON.stringify(details) : undefined,
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(body.detail ?? "Could not retry this audit.");
      }
      setRetryMessage("Retry started. The report will update as fresh results arrive.");
      await refresh();
    } catch (reason) {
      setRetryMessage(
        reason instanceof Error ? reason.message : "Could not retry this audit.",
      );
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

  if (!audit || audit.id !== auditId) {
    return error
      ? <main className="report-page"><p className="form-error">{error}</p><Link href="/">← Back to TrueEstate</Link></main>
      : <main className="report-page"><div className="loading-mark">T</div><p>Starting your property audit…</p></main>;
  }

  const record = audit.rera_record ?? {};
  const candidates = Array.isArray(record.candidates)
    ? record.candidates as Array<Record<string, unknown>>
    : [];
  const localityEvidence = audit.locality || [];
  const isRunning = ["queued", "running"].includes(audit.status);
  const score = audit.score?.score;
  const preliminaryScore = score == null
    ? audit.preliminary_assessment?.score
    : null;

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
          <p>{[audit.builder, audit.locality_name || audit.claims.locality, audit.city, audit.state].filter(Boolean).join(" · ") || "Project details are being extracted from the listing."}</p>
        </div>
        <div className="report-actions">
          <button className="outline-button" type="button" onClick={() => window.print()}>Print report</button>
          <button className="outline-button" type="button" onClick={() => void shareReport()}>Share report ↗</button>
          {["partial", "failed"].includes(audit.status) && (
            <button className="outline-button" type="button" onClick={() => void retryResearch()}>
              Retry research ↻
            </button>
          )}
          {typeof record.project_url === "string" && (
            <button className="outline-button" type="button" onClick={createWatch}>＋ Watch this project</button>
          )}
        </div>
      </div>
      {watchMessage && <div className="inline-notice" role="status">{watchMessage}</div>}
      {shareMessage && <div className="inline-notice" role="status">{shareMessage}</div>}
      {retryMessage && <div className="inline-notice" role="status">{retryMessage}</div>}
      {error && <div className="inline-notice error-notice" role="alert">{error}</div>}

      {isRunning && (
        <section className="progress-card">
          <div className="progress-head"><span className="live-dot pulse" /> AUDIT IN PROGRESS <span className="muted">{audit.status}</span></div>
          <div className="progress-list">
            {["extract", "rera", "news", "residents", "locality", "classification", "audit"].map((stage) => {
              const latest = [...audit.progress].reverse().find((item) => item.stage === stage);
              const label = stage === "rera" ? "Official RERA record" : stage === "audit" ? "Trust score" : stage === "residents" ? "Resident evidence" : stage === "locality" ? "Locality research" : stage === "news" ? "News coverage" : stage === "classification" ? "Evidence checks" : "Listing claims";
              return <div key={stage} className="progress-item"><span className={`progress-icon ${latest?.status ?? ""}`}>{latest?.status === "completed" ? "✓" : latest?.status === "failed" ? "!" : latest?.status === "skipped" ? "–" : "·"}</span><div><b>{label}</b><p>{latest?.message ?? "Waiting to start…"}</p></div></div>;
            })}
          </div>
        </section>
      )}

      {(audit.status === "needs_input" || audit.status === "unsupported") && (
        audit.status === "unsupported"
          ? <div className="inline-notice">Live RERA audits currently support Karnataka only. No official records were queried for {audit.state || "this state"}.</div>
          : <ManualDetailsForm audit={audit} onSubmit={(details) => void retryResearch(details)} />
      )}

      <div className="report-grid">
        <section className={`score-card risk-${(audit.score?.risk_band ?? "unknown").toLowerCase()}`}>
          <div className="card-kicker">
            {preliminaryScore == null ? "TRUEESTATE TRUST SCORE" : "PRELIMINARY EVIDENCE SCORE"}
          </div>
          <div className="score-number">{score ?? preliminaryScore ?? "—"}<span>{score == null && preliminaryScore == null ? "" : "/100"}</span></div>
          <div className="risk-label">
            {preliminaryScore != null
              ? "Incomplete · locality evidence only"
              : `${audit.score?.risk_band ?? "Pending"} risk`}
          </div>
          <div className="confidence-line">Confidence: <b>{audit.score?.confidence ?? "Low"}</b></div>
          <div className="score-track"><span style={{ width: `${score ?? preliminaryScore ?? 0}%` }} /></div>
          <p>
            {preliminaryScore != null
              ? `${audit.preliminary_assessment?.basis ?? "Calculated only from the verified evidence deductions shown in this report."} ${audit.preliminary_assessment?.limitations ?? "This is not a property trust score. The official RERA record is unverified, so registration, complaints, builder history, and completion dates are excluded."}`
              : score == null
                ? "A property trust score is unavailable until an official RERA record is verified. This does not mean the project is unregistered."
                : "Rule-based score from the evidence retrieved so far. It is not a verdict or legal advice."}
          </p>
        </section>

        <ReportPanel title="Listing claims" subtitle="What was provided or extracted">
          <dl className="detail-list">
            <div><dt>Project</dt><dd>{display(audit.claims.project_name)}</dd></div>
            <div><dt>Builder</dt><dd>{display(audit.claims.builder)}</dd></div>
            <div><dt>Property address</dt><dd>{display(audit.claims.property_address)}</dd></div>
            <div><dt>Locality</dt><dd>{display(audit.claims.locality)}</dd></div>
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
        ) : <p className="empty-state">{record.ambiguous ? "Several plausible records were returned. Select the matching project below to calculate a score." : record.found === false ? "An official Karnataka RERA record could not be verified. Portal or connection failures do not establish non-registration. See the Agent Console for attempt details." : isRunning ? "Waiting for the RERA portal result…" : "RERA record not retrieved. Check the Agent Console for the reason."}</p>}
        {typeof record.project_url === "string" && <a className="source-link" href={record.project_url} target="_blank" rel="noreferrer">Open official project record ↗</a>}
        {record.ambiguous === true && candidates.length > 1 && (
          <div className="candidate-list"><h3>Choose the matching project</h3>{candidates.map((candidate, index) => <button key={`${String(candidate.rera_id)}-${index}`} disabled={typeof candidate.project_url !== "string"} onClick={() => void selectCandidate(candidate)} className="candidate-row"><span><b>{display(candidate.project_name)}</b><small>{display(candidate.promoter)} · {display(candidate.rera_id)}</small></span><span>{typeof candidate.project_url === "string" ? "Use this →" : "No page link"}</span></button>)}</div>
        )}
      </ReportPanel>

      <ReportPanel title="Builder track record" subtitle="Other Karnataka RERA projects checked in parallel">
        {audit.builder_projects?.length ? (
          <div className="builder-table-wrap">
            <table className="builder-table">
              <thead><tr><th>Project</th><th>RERA ID</th><th>Status</th><th>Original completion</th><th>Revised completion</th><th>Extensions</th><th>Complaints</th><th>Official record</th><th>Public views</th></tr></thead>
              <tbody>{audit.builder_projects.map((project, index) => (
                <tr key={`${project.rera_id || project.project_name}-${index}`}>
                  <td>{project.project_name || "Not retrieved"}</td>
                  <td>{project.rera_id || "Not retrieved"}</td>
                  <td>{project.status || "Not retrieved"}</td>
                  <td>{project.original_completion_date || "Not retrieved"}</td>
                  <td>{project.revised_completion_date || "Not retrieved"}</td>
                  <td>{project.extensions_count ?? "Not retrieved"}</td>
                  <td>{project.complaints_count ?? "Not retrieved"}</td>
                  <td>{project.project_url ? <a href={project.project_url} target="_blank" rel="noreferrer">RERA ↗</a> : "Not retrieved"}</td>
                  <td>
                    {project.public_sources?.length ? (
                      <div className="public-source-links">
                        {project.public_sources.map((source, sourceIndex) => (
                          <a href={source.url} target="_blank" rel="noreferrer" key={`${source.url}-${sourceIndex}`}>
                            {source.source_name || source.title || `Public view ${sourceIndex + 1}`} ↗
                          </a>
                        ))}
                      </div>
                    ) : "No matching public views found."}
                  </td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        ) : <p className="empty-state">{isRunning ? "Searching Karnataka RERA for this builder, then checking up to five project records…" : audit.research_status?.builder?.message || "Builder portfolio search has not returned verified projects."}</p>}
        <ResearchStatus audit={audit} section="builder" />
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
        <ReportPanel title="Project & builder news" subtitle="Recent relevant public coverage">
          <EvidenceList items={audit.news} empty={researchEmpty(audit, "news", isRunning, "Searching recent project and builder news…")} />
          <ResearchStatus audit={audit} section="news" />
        </ReportPanel>
        <ReportPanel title="Resident & community evidence" subtitle="Project and locality mentions · source-checked, not independently verified">
          <section className="locality-evidence-group">
            <h3 className="evidence-group-title">Project-related mentions</h3>
            <EvidenceList
              items={audit.residents.filter((item) => item.source_scope !== "locality")}
              empty={researchEmpty(audit, "residents", isRunning, "Searching public project forums…")}
            />
          </section>
          <section className="locality-evidence-group">
            <h3 className="evidence-group-title">Locality-related mentions</h3>
            <EvidenceList
              items={localityEvidence.filter((item) => item.source_type === "resident_forum")}
              empty={researchEmpty(audit, "locality", isRunning, "Searching community mentions for this locality…")}
            />
          </section>
          <ResearchStatus audit={audit} section="residents" />
        </ReportPanel>
      </div>
      <ReportPanel
        title="Locality research"
        subtitle={`Public neighborhood reports around ${audit.locality_name || audit.claims.locality || audit.city || "the project"} · source-checked, not a safety guarantee`}
      >
        <section className="locality-evidence-group">
          <h3 className="evidence-group-title">Local news</h3>
          <EvidenceList
            items={localityEvidence.filter((item) => item.source_type === "news")}
            empty="No verified locality-focused news results."
          />
        </section>
        <section className="locality-evidence-group">
          <h3 className="evidence-group-title">Other public sources</h3>
          <EvidenceList
            items={localityEvidence.filter((item) => item.source_type !== "news" && item.source_type !== "resident_forum")}
            empty="No additional verified public sources."
          />
        </section>
        <ResearchStatus audit={audit} section="locality" />
        {audit.research_status?.classification?.status !== "completed" && !isRunning && (
          <p className="inline-notice">
            {audit.research_status?.classification?.message ?? "Source classification was not completed; unverified results do not affect findings or scoring."}
          </p>
        )}
      </ReportPanel>

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
      {item.classification?.summary && <p className="evidence-summary"><b>AI summary:</b> {item.classification.summary}</p>}
      {item.classification?.verified && item.classification.summary
        ? <p className="source-quote"><b>Supporting quote:</b> {item.classification.excerpt}</p>
        : <p>{item.classification?.verified ? item.classification.excerpt : item.excerpt || item.snippet || "No readable excerpt returned."}</p>}
      {item.classification && <p className="classification-label">{item.classification.verified ? item.classification.relevant ? item.classification.issue_category ? `Reported topic: ${item.classification.issue_category.replaceAll("_", " ")}` : item.classification.topic || (item.classification.negative ? "Relevant negative coverage" : "Relevant source") : "Source quote verified, but not matched to this project or locality" : "No verifiable source quote; excluded from scoring"}</p>}
      <div className="evidence-meta"><span>{[item.publisher || item.site_name || "Public web result", item.date, item.source_scope === "locality" ? "Locality" : "Project / builder", item.retrieved_at ? `Retrieved ${new Date(item.retrieved_at).toLocaleDateString()}` : null].filter(Boolean).join(" · ")}</span>{item.url && <a href={item.url} target="_blank" rel="noreferrer">Source ↗</a>}</div>
      {(item.tinyfish_tool || item.trace_id) && <p className="finding-meta">{item.tinyfish_tool || "Search"} · Trace/run ID {item.trace_id || "not returned"}</p>}
    </article>
  ))}</div>;
}

function researchEmpty(
  audit: Audit,
  section: string,
  isRunning: boolean,
  runningMessage: string,
) {
  if (isRunning) return runningMessage;
  const status = audit.research_status?.[section];
  if (status?.status === "failed") {
    return `Research could not be completed: ${status.message || "provider unavailable"}`;
  }
  if (status?.status === "skipped") {
    return `Research was not run: ${status.message || "details or configuration are missing"}`;
  }
  if (status?.status === "completed" && !status.count) {
    return "Search completed but returned no matching public results. Coverage may be incomplete; this is not evidence that no issue exists.";
  }
  return "No readable public sources were retrieved for this section.";
}

function ResearchStatus({ audit, section }: { audit: Audit; section: string }) {
  const status = audit.research_status?.[section];
  if (!status?.message || status.status === "pending") return null;
  return <p className="finding-meta">{status.message}</p>;
}
