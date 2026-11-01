# TrueEstate

> **Claude can research a project once. TrueEstate audits the listing against the official record, checks the builder's history across many RERA pages in parallel, and keeps watching after you have bought.**

*Built for Swim with TinyFish Finale · Team CodeBreak (Lead: Swapnil Patil · Scaler School of Technology)*

---

## 1. Overview

**TrueEstate** is an autonomous web agent platform that performs end-to-end due diligence on real estate properties. When a buyer pastes a property listing URL (NoBroker, MagicBricks, etc.) or enters project details, TrueEstate:
1. **Extracts listing claims** (advertised possession date, pricing, area, claimed RERA ID, builder, locality).
2. **Navigates state RERA portals** to retrieve official legal records (status, registered completion dates, formal extensions, and consumer complaints).
3. **Fans out across the promoter's portfolio** in parallel browser sessions to evaluate builder delivery track records and systemic delays.
4. **Mines resident forums & local news** for hyper-local civic issues (waterlogging, structural defects, power/water supply, traffic disputes) requiring multi-source verification.
5. **Computes an Explainable Trust Score (0–100)** with deterministic rule-based deductions, where 100% of findings link directly to source evidence, timestamps, and TinyFish run IDs.
6. **Maintains active watch monitors** post-audit to alert buyers to changes in RERA filings or breaking local news via automated cron schedules.

---

## 2. Problem & Impact

- **Aggregators sell upside:** Portals like NoBroker and MagicBricks do not surface RERA extensions, litigations, consumer complaints, or civic infrastructure hazards.
- **Form-driven RERA silos:** State government portals require complex manual form inputs, CAPTCHAs, and multi-tab navigation across PDFs.
- **Scattered community reality:** Evidence of chronic waterlogging, power outages, and builder disputes is buried across Reddit threads, local publications, and resident forums.
- **High buyer exposure:** Over 5.08 lakh homes remain undelivered across 44 Indian cities; 98% of surveyed buyers cite timely possession as their top criterion, yet lack tools for automated legal verification.

---

## 3. Why TinyFish is the Core Engine

TrueEstate leverages TinyFish as its core operating engine across 4 primary capabilities:

| Capability | Problem Solved | TinyFish Feature Used |
| :--- | :--- | :--- |
| **Portal Automation** | Driving form-gated Karnataka RERA searches, extracting deep project tabs, and handling dynamic UI flows. | `run_web_automation` with structured `output_schema`, stealth browser profile, and timeout safeguards. |
| **Portfolio Fan-Out** | Checking 5–8 builder projects simultaneously across multiple browser tabs without rate-limiting bottlenecks. | Parallel automation sessions (`run_web_automation` / async patterns) aggregating live completion metrics. |
| **Scoped Research** | Locating timely news disputes and resident threads while eliminating irrelevant web noise. | `search` (domain-type filtering `news`, `web`, `include_domains: ["reddit.com"]`, `after_date`, geo-location `IN`). |
| **Clean Page Ingestion** | Extracting clean text from heavy JS-rendered listing portals and news sites. | `fetch_content` (markdown format with purpose-directed prompt scoping). |
| **Post-Audit Monitoring** | Watching RERA project pages and news queries for unannounced deadline revisions or complaints. | `create_monitor` (`fetch` and `search` types with cron schedules and webhook triggers). |

---

## 4. Feature Matrix (F1 – F12)

### F1. Dual Audit Input (URL & Manual)
- Paste any listing URL or manually enter project name, builder, city, state, and optional RERA ID.
- Automatically handles fallback to manual input if a listing site blocks crawling or lacks essential parameters.

### F2. Listing Claim Extraction
- Ingests listing content using `fetch_content` with an LLM parser (`ListingClaims`).
- Automatic fallback to TinyFish stealth browser automation (`run_web_automation`) for anti-bot protected listing pages.
- Extracts project name, promoter, locality, property address, carpet area, advertised possession date, and claimed RERA ID without hallucination.

### F3. Official RERA Record Audit (Hero Feature)
- Drives the state RERA portal (Karnataka RERA) using structured goal templates and strict JSON `output_schema`.
- Extracts registration status, promoter identity, original completion date, revised completion date, count of extensions, complaint counts, and the canonical project URL.
- Resilient multi-tier strategy: attempts exact registration ID search, falls back to project name search, and queries public references if initial portal search yields no direct hits.

### F4. Deterministic Contradiction Detector
- 100% rule-based backend comparison between listing claims and official RERA records (no LLM hallucination in scoring):
  - **Timeline Discrepancies:** Flags differences between advertised possession date and RERA revised completion date (>6 months = Warning; >18 months = Critical).
  - **RERA ID Inconsistencies:** Flags missing or mismatched registration IDs.
  - **Builder/Project Mismatches:** Normalized string comparison detects misattributed builder or sub-brand entities.
  - **Lapsed/Revoked Status:** Flags inactive, lapsed, or non-registered projects.

### F5. Builder Portfolio Fan-Out
- Discovers up to 5 additional projects registered by the promoter.
- Navigates and verifies each project page in parallel to extract status, extensions, and complaints.
- Collects live public references for each portfolio item and calculates portfolio-wide extension rates and delay averages.

### F6. Local News Intelligence
- Queries indexed Indian publications (`domain_type: "news"`, within the past 24 months) for builder disputes, delays, NCLT/RERA proceedings, and land issues.
- Fetches article text and classifies relevance via LLM, enforcing verbatim excerpt matching against source text before citations are accepted.

### F7. Resident Sentiment & Civic Risk Mining
- Scans `reddit.com` and local civic forums for hyper-local hazards: waterlogging, flooding, sewage, power cuts, water quality, and structural cracks.
- **Two-Source Verification Rule:** Civic issues require at least 2 independent source threads to impact score deductions; single mentions are tagged as unverified observations.

### F8. Verifiable Agent Console
- Dedicated inspection drawer on every report displaying every TinyFish call:
  - Tool name, goal/query, target URL, run ID, and TrueEstate trace ID.
  - Live execution duration, raw request/response JSON payload, and status.
  - **LIVE** vs. **CACHED** transparency badges—ensuring no simulated data is presented as live web activity.

### F9. Explainable Trust Score
- Multi-dimensional scoring engine starting at 100 with clear deductions:
  - Unregistered / Lapsed RERA: **-40 pts**
  - Timeline mismatch (>18 mos / >6 mos): **-25 / -15 pts**
  - Official extensions: **-8 pts each** (max -20)
  - Consumer complaints: **-3 pts each** (max -15)
  - Builder delayed portfolio ratio: up to **-15 pts**
  - Verified resident civic issues: up to **-10 pts**
  - Verified adverse builder news: up to **-10 pts**
- Risk bands: **Low (80–100)**, **Moderate (60–79)**, **High (40–59)**, **Critical (<40)**.
- Independent Confidence Metric: **High**, **Medium**, or **Low** based on agent retrieval completeness.

### F10. Candidate Disambiguation
- When portal search returns multiple plausible projects under similar phase names, the UI presents an interactive candidate picker so the user selects the exact phase before completing the audit.

### F11. Project Watchlist & Scheduled Monitors
- **"Watch this project"** creates automated recurring monitors via TinyFish:
  1. `fetch` monitor on the verified RERA detail page URL.
  2. `search` monitor on project & builder news.
- Stores baseline snapshots and compares subsequent webhook payloads to alert users of revised possession dates, new complaints, or breaking news.

### F12. Shareable Evidence Report & Print View
- Read-only permalinks with share button.
- Clean `@media print` CSS stylesheet formatting the audit into an executive due diligence dossier suitable for PDF export.

---

## 5. System Architecture & Pipeline

```
                     +---------------------------------------+
                     |              User Input               |
                     |  (Listing URL or Project/City Form)   |
                     +---------------------------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |         F2: Extract Claims            |
                     |  fetch_content -> LLM / Stealth Auto  |
                     +---------------------------------------+
                                         |
         +-------------------------------+-------------------------------+
         |                               |                               |
         v                               v                               v
+------------------+           +-------------------+           +-------------------+
|  Lawyer Agent    |           |    News Agent     |           |  Resident Agent   |
|  F3: RERA Portal |           |  F6: Scoped News  |           |  F7: Civic Forums |
|  F5: Builder     |           |      Search +     |           |     Search +      |
|      Fan-Out     |           |   fetch_content   |           |   fetch_content   |
+------------------+           +-------------------+           +-------------------+
         |                               |                               |
         +-------------------------------+-------------------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |       F4: Contradiction Engine        |
                     |   Deterministic Claim vs RERA Match   |
                     +---------------------------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |        F9: Trust Score Engine         |
                     |  0-100 Score, Deductions & Evidence   |
                     +---------------------------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |     SSE Streaming (Progress Events)   |
                     |     FastAPI -> Next.js Live View      |
                     +---------------------------------------+
                                         |
                                         v
                     +---------------------------------------+
                     |      F11: Post-Audit Watchlist        |
                     |  create_monitor -> Webhook Diffing    |
                     +---------------------------------------+
```

### Technology Stack
- **Backend:** Python 3.11+, FastAPI, `asyncio` parallel pipelines, `httpx`, Pydantic v2.
- **Database:** SQLite with WAL mode (`trueestate.db`) storing Audits, Runs, Events, and Watches.
- **Agent Integration:** TinyFish REST API (`automation`, `search`, `fetch`, `monitors`).
- **Classification & Extraction:** OpenRouter API (`openrouter/free` router or custom specified LLM).
- **Frontend:** Next.js 16 (App Router), React 19, TypeScript, TailwindCSS v4, Server-Sent Events (SSE).

---

## 6. API Reference

| Method | Endpoint | Purpose |
| :--- | :--- | :--- |
| `POST` | `/api/audits` | Enqueues a new audit pipeline from URL or manual details. |
| `GET` | `/api/audits/{id}` | Fetches full audit report (claims, RERA record, score, evidence). |
| `GET` | `/api/audits/{id}/events` | Real-time Server-Sent Events (SSE) stream of agent progress. |
| `GET` | `/api/audits/{id}/runs` | Agent Console execution log (RunLog entries with raw payloads). |
| `POST` | `/api/audits/{id}/retry` | Retries failed or partial audit stages with optional overrides. |
| `POST` | `/api/audits/{id}/select-candidate` | Selects a specific RERA project from an ambiguous candidate list. |
| `POST` | `/api/watches` | Provisions TinyFish fetch and search monitors for an audited project. |
| `GET` | `/api/watches` | Lists monitored projects, last check times, and detected diffs. |
| `POST` | `/api/webhooks/tinyfish` | Ingests TinyFish monitor webhooks and diffs against baseline. |
| `GET` | `/api/health` | Service health status and API configuration check. |

---

## 7. Getting Started

### Prerequisites
- Python 3.11 or newer
- Node.js 20.9 or newer (tested with v24.x)
- TinyFish API Key ([tinyfish.ai](https://tinyfish.ai))
- OpenRouter API Key ([openrouter.ai](https://openrouter.ai))

---

### Backend Setup

1. Navigate to the backend directory:
   ```bash
   cd backend
   ```

2. Create and activate a virtual environment:
   ```bash
   # Windows PowerShell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1

   # Linux / macOS
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables:
   ```bash
   # Copy example environment configuration
   cp .env.example .env
   ```
   Edit `.env` and set your credentials:
   ```ini
   TINYFISH_API_KEY="sk-tinyfish-your-key-here"
   OPENROUTER_API_KEY="sk-or-v1-your-openrouter-key-here"
   OPENROUTER_MODEL="openrouter/free"
   DATABASE_PATH="./data/trueestate.db"
   FRONTEND_ORIGIN="http://localhost:3000"

   # Required only for live public webhook monitoring:
   PUBLIC_BASE_URL="https://your-public-backend.domain.com"
   WEBHOOK_SECRET="your-secure-webhook-secret"
   ```

5. Run the FastAPI development server:
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

---

### Frontend Setup

1. Open a new terminal and navigate to the frontend directory:
   ```bash
   cd frontend
   ```

2. Install dependencies:
   ```bash
   npm install
   ```

3. Configure environment variables:
   ```bash
   # Create .env.local if needed (defaults to http://localhost:8000)
   echo "NEXT_PUBLIC_API_URL=http://localhost:8000" > .env.local
   ```

4. Start the Next.js development server:
   ```bash
   npm run dev
   ```

5. Open [http://localhost:3000](http://localhost:3000) in your browser.

---

## 8. Running Automated Tests

TrueEstate includes test suites covering scoring logic, API routes, contradiction thresholds, classification guards, and fallback behaviors.

### Run Backend Tests
```bash
cd backend
pytest tests/ -v
```

### Run Frontend Lint & Build
```bash
cd frontend
npm run lint
npm run build
```

---

## 9. 4-Minute Demo Script

1. **Problem (30s):** Open an aggregator listing (e.g. NoBroker). Note that the listing advertises possession by December 2026 and claims the builder is reputable.
2. **Launch Live Audit (60s):** Paste the listing URL into TrueEstate. Watch the SSE progress card activate:
   - Extract agent parses listing claims.
   - Lawyer agent drives Karnataka RERA.
   - Builder agent checks portfolio projects in parallel.
   - News and community agents scan news and Reddit.
3. **The Reveal (60s):** The contradiction card highlights a major timeline mismatch:
   - *Listing advertised:* December 2026.
   - *Official RERA record:* December 2028 after 2 formal extensions and 4 active consumer complaints.
   - *Builder track record:* 3 of 5 checked promoter projects suffered extensions or lapsed status.
   - *Community evidence:* 2 independent Reddit threads verified waterlogging and basement seepage during monsoons.
4. **Verifiable Proof in Agent Console (45s):**
   - Open the Agent Console panel.
   - Inspect the real TinyFish `run_id`, the exact goal string passed to the browser automation agent, and the returned raw JSON payload.
   - Point out the **LIVE** badge verifying execution against the real portal.
5. **Ongoing Protection (45s):**
   - Click **"＋ Watch this project"**.
   - Navigate to the **Watchlist** to view the active cron monitor and baseline state snapshot.

---

## 10. Legal & Ethical Safeguards

- **Public Data Only:** TrueEstate accesses only publicly accessible state government records, news articles, and public discussion threads.
- **No Fabricated Data:** If a portal or search fails, the application marks the section as *Not Retrieved* and lowers confidence rather than guessing.
- **Fair Wording:** Findings cite explicit sources (e.g., *"Per Karnataka RERA record..."*, *"Reported in local coverage..."*) and do not publish slanderous or accusatory conclusions.
- **Informational Disclaimer:** TrueEstate reports are informational due diligence tools and do not constitute formal legal or financial advice.
