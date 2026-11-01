"use client";

import { FormEvent, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";

const API = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export default function Home() {
  const router = useRouter();
  const [mode, setMode] = useState<"url" | "manual">("url");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setLoading(true);
    const form = new FormData(event.currentTarget);
    const value = (name: string) => String(form.get(name) ?? "").trim();
    const body =
      mode === "url"
        ? { listing_url: value("listing_url") }
        : {
            project_name: value("project_name"),
            builder: value("builder"),
            city: value("city"),
            state: value("state"),
            rera_id: value("rera_id") || null,
            possession_date: value("possession_date") || null,
            carpet_area: value("carpet_area") || null,
            price: value("price") || null,
          };
    try {
      const response = await fetch(`${API}/api/audits`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const result = await response.json();
      if (!response.ok) {
        throw new Error(
          typeof result.detail === "string"
            ? result.detail
            : "Could not start the audit.",
        );
      }
      router.push(`/report/${result.id}`);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? `${reason.message} Check that the TrueEstate API is running.`
          : "Could not reach the TrueEstate API.",
      );
      setLoading(false);
    }
  }

  return (
    <main>
      <header className="topbar">
        <Link className="brand" href="/">
          <span className="brand-mark">T</span>
          <span>TrueEstate</span>
        </Link>
        <nav>
          <a href="#how-it-works">How it works</a>
          <Link className="nav-cta" href="/watchlist">
            Watchlist <span aria-hidden="true">↗</span>
          </Link>
        </nav>
      </header>

      <section className="hero">
        <div className="hero-copy">
          <div className="eyebrow"><span className="live-dot" /> PROPERTY DUE DILIGENCE</div>
          <h1>Before you buy,<br />know what’s <em>behind</em> the listing.</h1>
          <p className="hero-description">
            Compare a property listing with official RERA records, builder
            history and public reporting. Every finding is tied to its source.
          </p>
          <div className="proof-points">
            <span><b>01</b> Official records</span>
            <span><b>02</b> Evidence-linked findings</span>
            <span><b>03</b> Ongoing monitoring</span>
          </div>
        </div>

        <div className="audit-card">
          <div className="card-kicker">START A PROPERTY AUDIT</div>
          <h2>Let’s look closer.</h2>
          <p className="muted">Start with a listing or enter project details.</p>
          <div className="mode-tabs" role="tablist" aria-label="Audit input">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "url"}
              className={mode === "url" ? "active" : ""}
              onClick={() => setMode("url")}
            >
              Listing URL
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "manual"}
              className={mode === "manual" ? "active" : ""}
              onClick={() => setMode("manual")}
            >
              Enter details
            </button>
          </div>
          <form onSubmit={submit} className="audit-form">
            {mode === "url" ? (
              <label>
                PROPERTY LISTING LINK
                <input
                  type="url"
                  name="listing_url"
                  placeholder="https://www.nobroker.in/..."
                  required
                />
              </label>
            ) : (
              <>
                <label>
                  PROJECT NAME
                  <input name="project_name" placeholder="e.g. Project name" required minLength={2} />
                </label>
                <div className="form-grid">
                  <label>
                    BUILDER
                    <input name="builder" placeholder="Builder / promoter" required minLength={2} />
                  </label>
                  <label>
                    CITY
                    <input name="city" placeholder="City" required minLength={2} />
                  </label>
                </div>
                <div className="form-grid">
                  <label>
                    STATE
                    <select name="state" defaultValue="Karnataka">
                      <option>Karnataka</option>
                    </select>
                  </label>
                  <label>
                    RERA ID <span className="optional">OPTIONAL</span>
                    <input name="rera_id" placeholder="If listed" />
                  </label>
                </div>
                <div className="form-grid">
                  <label>
                    LISTING POSSESSION <span className="optional">OPTIONAL</span>
                    <input name="possession_date" type="date" />
                  </label>
                  <label>
                    PRICE <span className="optional">OPTIONAL</span>
                    <input name="price" placeholder="As advertised" />
                  </label>
                </div>
              </>
            )}
            {error && <p className="form-error" role="alert">{error}</p>}
            <button className="primary-button" type="submit" disabled={loading}>
              {loading ? "Starting your audit…" : "Audit this property"}
              {!loading && <span aria-hidden="true">→</span>}
            </button>
            <p className="form-note">
              Public sources only. No account or personal information required.
            </p>
          </form>
        </div>
      </section>

      <section className="trust-strip" id="how-it-works">
        <div><span className="strip-number">01</span><div><b>Claims, checked</b><p>What the listing says vs. the official record.</p></div></div>
        <div><span className="strip-number">02</span><div><b>Sources, not guesses</b><p>See retrieval status, source links and agent runs.</p></div></div>
        <div><span className="strip-number">03</span><div><b>Stay in the loop</b><p>Monitor a project page for changes over time.</p></div></div>
      </section>
      <footer className="site-footer">
        <span>TRUEESTATE <span className="footer-divider">/</span> BUY WITH YOUR EYES OPEN</span>
        <span>Informational only. Not legal advice.</span>
      </footer>
    </main>
  );
}
