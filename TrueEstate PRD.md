# TrueEstate: Product Requirements Document

*Team CodeBreak · Lead: Swapnil Patil · Scaler School of Technology · Swim with TinyFish finale, 1 Nov 2026*

## 1. Summary

TrueEstate is a web agent product that audits a property listing against official records and live community evidence, then keeps watching the project after the audit. A buyer pastes a listing URL (NoBroker, MagicBricks) or enters a project name, builder and city. TrueEstate extracts what the listing *claims*, drives the state RERA portal to get what the legal record *says*, checks the builder's other projects, scans news and resident forums, and returns an explainable Trust Score where every deduction links to evidence.

**One-line pitch:** Claude can research a project once. TrueEstate audits the listing against the official record, checks the builder's history across many RERA pages in parallel, and keeps watching after you have bought.

This PRD describes the product as submitted in Round 1 (AI-powered RERA and community due diligence) and defines how it is built so that TinyFish is the core engine, not a wrapper.

## 2. Problem

- Listings sell the upside. Aggregator sites do not surface RERA extensions, complaints, builder history or neighbourhood problems.
- RERA data sits behind form-driven, state-specific portals. Finding one project means searching, selecting, opening tabs and reading PDFs.
- Livability evidence (waterlogging, power cuts, traffic) is scattered across Reddit, local news and forums.
- Buyers spend weeks cross-checking and still miss warning signs. Context from the deck: about 5.08 lakh homes are undelivered across 44 cities, and 98% of surveyed buyers prioritise timely completion.

## 3. Goals and non-goals

### Goals

1. Detect contradictions between what a listing claims and what the official record shows.
2. Produce a decision-ready report in a few minutes, with every finding traceable to a source.
3. Show the builder's track record, not just one project.
4. Continue to monitor the project after the audit and alert on changes.
5. Make the TinyFish usage visible and verifiable inside the product (run IDs, raw outputs, live or cached labels).

### Non-goals

- Not legal advice and not a verdict on any builder. Findings are reported as "per source X".
- Not a listing search engine or marketplace.
- Not all-India coverage on day one. The finale build targets one state in depth, with a clear extension path.
- No fabricated data. If a portal fails, the product shows the failure.

### Success metrics (for the demo and judging)

| Metric | Target |
| --- | --- |
| End-to-end audit time for a known project | Under 4 minutes live |
| Findings with a source link, timestamp and run ID | 100% |
| Live run succeeds on stage, or cached fallback clearly labelled | Yes |
| Distinct TinyFish capabilities visibly used | 4 (automation, search, fetch, monitor) |

## 4. Users

| Persona | Need |
| --- | --- |
| First-time homebuyer (primary) | Know whether a project is safe before paying a booking amount |
| Buyer's family or adviser | A shareable, evidence-linked report |
| Existing buyer (secondary) | Be alerted if the RERA completion date or complaints change |

## 5. Why TinyFish is central

A generic chat with a search tool can summarise public pages once. TrueEstate depends on things that need a real web agent:

| Need | Why plain search or a scraper is not enough | TinyFish capability |
| --- | --- | --- |
| Navigate the RERA portal (search form, result list, project detail, complaints tab) | Content appears only after multi-step interaction | `run_web_automation` with a plain-English goal and `output_schema` |
| Repeat that navigation for every project of one builder | Needs many parallel browser sessions | Parallel `run_web_automation` runs (async variant plus `wait_for_run` / `batch_status`) |
| Find resident complaints and news | Needs scoped, fresh search | `search` with `domain_type`, `include_domains`, `after_date`, `purpose` |
| Read listing pages and articles as clean text | JS-rendered pages | `fetch_content` (markdown, selectors, links) |
| Watch a project after the audit | Needs scheduled re-checks | `create_monitor` (fetch or search type, cron, webhook) |

The product differs from "Claude plus TinyFish MCP" in four ways: it is a persistent system (watchlist and monitors), it computes a deterministic diff between claim and record, it fans out across a builder's portfolio, and it exposes the agent run data in the UI.

## 6. Product scope and features

Priority key: **P0** must ship for the finale, **P1** ship if time allows, **P2** roadmap.

### F1. Audit input (P0)

**What:** Two input modes on the home page: paste a listing URL, or fill a manual form (project name, builder, city, state, optional RERA ID). The manual form is also the fallback if listing extraction fails.

**How it works:** The frontend calls `POST /api/audits`. The backend creates an audit record with a status of `queued` and starts the pipeline.

### F2. Listing claim extraction (P0)

**What:** From a listing URL, extract the claims the seller makes: project name, builder, locality, city, state, advertised possession date, RERA ID if shown, carpet area, price.

**How it works:**

1. `fetch_content` on the listing URL, format `markdown`.
2. An LLM call converts the text to a strict JSON object (`ListingClaims`). The LLM is told to return `null` for anything not present and never to guess.
3. If fetch returns an error or empty content, the backend retries with `run_web_automation` using the `stealth` browser profile and a goal such as "open this listing and return the project name, builder, possession date and RERA ID". If that also fails, the UI asks the user to fill the manual form.

**TinyFish use:** `fetch_content`, with `run_web_automation` as the fallback.

### F3. RERA record audit (P0, hero feature)

**What:** Drive the state RERA portal and return the official record for the project: registration status, RERA ID, promoter name, proposed and revised completion dates, number of extensions, registered complaints, and the project page URL.

**How it works:**

1. The backend picks the portal config for the state (Karnataka RERA for the finale; the config holds the portal URL and a goal template).
2. It calls `run_web_automation` with the portal URL, a goal like "Search for the project \<name> by \<builder> in \<city>. Open the best matching project page. Return the fields in the schema. If several projects match, return the top three candidates instead", and an `output_schema` so the result is structured JSON.
3. The backend validates the JSON (types, date formats). Candidate lists go to a disambiguation step in the UI (F10).
4. The run ID, duration, status and raw output are stored for the Agent Console (F8).

**TinyFish use:** `run_web_automation` with `output_schema`, optional `capture_config` for screenshots, `agent_config.max_duration_seconds` to cap runtime, and `proxy_config` with an Indian exit country if the portal geo-restricts.

**Example `output_schema` (simplified):**

```json
{
  "type": "object",
  "properties": {
    "found": {"type": "boolean"},
    "rera_id": {"type": "string"},
    "project_name": {"type": "string"},
    "promoter": {"type": "string"},
    "status": {"type": "string"},
    "original_completion_date": {"type": "string"},
    "revised_completion_date": {"type": "string"},
    "extensions_count": {"type": "number"},
    "complaints_count": {"type": "number"},
    "project_url": {"type": "string"},
    "candidates": {"type": "array", "items": {"type": "object"}}
  },
  "required": ["found"]
}
```

### F4. Claim vs. record contradiction detector (P0)

**What:** A deterministic comparison between F2 (claims) and F3 (record). Each mismatch becomes a finding with severity.

**How it works (pure backend code, no LLM):**

- Possession date: listing date vs. RERA revised completion date. Gap over 6 months is a warning; over 18 months is critical.
- RERA ID: listing shows none, or shows one that does not match the record.
- Builder or project name mismatch (normalised string comparison).
- Status: record shows lapsed, revoked or not registered.

Example output: "Listing says possession Dec 2026. RERA record shows Dec 2028 after 2 extensions."

**TinyFish use:** consumes the structured output of F3, which is why `output_schema` matters.

### F5. Builder portfolio fan-out (P1, strong differentiator)

**What:** Pull the builder's other registered projects and compute a track record: how many were extended, how many have complaints, how many are lapsed.

**How it works:**

1. One `run_web_automation` run searches the portal by promoter name and returns a list of project names, IDs and URLs (capped at 5 to 8 for time).
2. The backend starts one async run per project (`run_web_automation_async`), each with the same `output_schema` as F3.
3. It polls with `batch_status` (up to 8 run IDs per call) or `wait_for_run`, and aggregates the results as they finish. The UI shows each project row filling in live.
4. Metrics: extension rate, average delay (revised minus original), total open complaints.

**TinyFish use:** parallel browser sessions, which is the capability a single chat session cannot match.

### F6. Local news context (P0)

**What:** Recent reporting tied to the builder or project location (delays, disputes, flooding, regulatory action).

**How it works:**

1. `search` with `domain_type: "news"`, queries such as `"<builder>" "<project>" delay OR complaint OR RERA`, `after_date` set to about 24 months ago, `location: "IN"`, and a `purpose` string describing the audit.
2. `fetch_content` on the top 3 to 5 article URLs.
3. An LLM classifies each article (relevant or not, topic, sentiment) and must return a short supporting excerpt. Articles that do not mention the builder or locality are dropped.

**TinyFish use:** `search` (news mode) and `fetch_content`.

### F7. Resident sentiment mining (P0)

**What:** Real resident complaints about waterlogging, power cuts, water supply, structural issues and traffic for the society or locality.

**How it works:**

1. `search` with `include_domains` set to `reddit.com` plus city-specific communities, queries such as `"<society>" waterlogging`, `"<society>" seepage`, `"<locality>" power cuts`.
2. `fetch_content` on the top threads.
3. An LLM tags each thread with issue categories and a date, with excerpts. Threads older than a configurable cutoff are down-weighted.
4. Findings need at least two independent threads before they count toward the score. A single post is shown as "unverified mention".

**TinyFish use:** `search` with domain scoping and `fetch_content`.

### F8. Agent Console (P0, judging criteria 3 and 4)

**What:** A panel on the report page showing exactly what TinyFish did. For each call: tool name, the goal or query sent, target URL, run ID, status, start time, duration, a collapsible raw JSON output, screenshots if captured, and a **LIVE** or **CACHED** badge.

**How it works:** every TinyFish call goes through one backend wrapper that records a `RunLog` row (see data model). The console reads these rows. Failed runs are shown in red with the error, not hidden.

**Why it matters:** judges can see the work is real, and you can explain the technical details (async runs, schema output, parallelism) from the UI itself.

### F9. Explainable Trust Score (P0)

**What:** A 0 to 100 score with a risk band, a confidence indicator and an itemised list of deductions, each linked to its evidence.

**How it works:** rule-based, computed in backend code (details in section 9). The LLM never produces the number.

### F10. Candidate disambiguation (P1)

**What:** If the RERA search returns multiple plausible projects, the UI shows the candidates (name, promoter, RERA ID) and the user picks one. The audit continues with the selected project.

### F11. Watchlist and monitors (P0 for one monitor, P1 for full watchlist)

**What:** A "Watch this project" button. TrueEstate creates monitors so the user is alerted if the RERA page changes (completion date, complaint count) or if new news appears.

**How it works:**

1. A `create_monitor` call of type `fetch` on the RERA project page URL, with a `purpose` such as "completion date or complaints count changes", a cron schedule (daily by default) and a `webhook_url` pointing to `POST /api/webhooks/tinyfish`.
2. A second monitor of type `search` for `"<builder>" "<project>"` news.
3. `create_monitor` returns a baseline result immediately. The backend stores it, and later webhook payloads are compared against it.
4. The Watchlist page lists monitored projects, last check time and detected changes.

**TinyFish use:** `create_monitor` (fetch and search types), schedule and webhook delivery.

### F12. Shareable report and export (P1)

**What:** A read-only link and a print-friendly view of the report with evidence links and a disclaimer.

## 7. User flow

1. User opens the home page and pastes a listing URL or fills the manual form.
2. The report page opens immediately in a "running" state and streams progress.
3. Stage 0 (Extract) finishes in about a minute: claims card appears.
4. Stage 1 (Investigate) runs the RERA agent, builder fan-out, news agent and resident agent in parallel. Each panel fills in as it completes.
5. Stage 2 (Deliver): contradictions are computed, the Trust Score is rendered with its evidence list.
6. The user can open the Agent Console, share the report or click **Watch this project**.

## 8. System architecture

### Components

| Component | Technology | Responsibility |
| --- | --- | --- |
| Frontend | Next.js, React, TailwindCSS | Input, live report, Agent Console, watchlist |
| Backend | Python, FastAPI, asyncio | Orchestration, TinyFish wrapper, scoring, storage |
| Agent layer | TinyFish API | Browsing, search, extraction, monitoring |
| LLM | Any JSON-capable model | Claim extraction and text classification only |
| Storage | SQLite for the finale (Postgres later) | Audits, findings, run logs, watches |

The TinyFish API key lives only on the backend, in an environment variable, and is never sent to the browser.

### Pipeline

```
Extract (F2)
   |
   v
+-----------------------------------------------+
| Investigate (parallel, asyncio.gather)        |
|  - Lawyer agent:  RERA record (F3) -> builder |
|                   fan-out (F5)                |
|  - News agent:    search + fetch (F6)         |
|  - Resident agent: search + fetch (F7)        |
+-----------------------------------------------+
   |
   v
Deliver: diff (F4) -> score (F9) -> JSON -> UI
```

### Progress streaming

The backend exposes `GET /api/audits/{id}/events` as Server-Sent Events. Each agent emits `started`, `progress`, `completed` or `failed` events. The frontend renders panels as events arrive. This is the cheapest way to make the parallel agents visible.

### Single TinyFish wrapper

All calls go through one module (for example `tinyfish_client.py`) that:

- adds auth headers and timeouts,
- generates a fresh UUID v4 `session_id` for every automation call (never reused),
- writes a `RunLog` row before and after each call,
- retries once on timeout (a timeout does not prove failure; check `get_run` or `wait_for_run` before re-running),
- returns a typed result or a typed error.

## 9. Trust Score specification

The score starts at 100. Deductions are applied by backend code. Values below are the starting configuration and can be tuned.

| Rule | Condition | Deduction |
| --- | --- | --- |
| Not registered | Project not found in RERA or status not active | -40 (critical) |
| Possession mismatch | Listing date vs. RERA revised date: over 6 months / over 18 months | -15 / -25 |
| Deadline extensions | Per extension, capped | -8 each, max -20 |
| Open complaints | Per complaint, capped | -3 each, max -15 |
| Builder track record | Share of builder's checked projects that were extended or lapsed | up to -15 |
| Livability evidence | Two or more independent sources on waterlogging, power or structural issues | up to -10 |
| Negative news | Relevant, recent, builder-specific negative coverage | up to -10 |

**Bands:** 80 to 100 Low risk · 60 to 79 Moderate · 40 to 59 High · below 40 Critical.

**Confidence (separate from score):** High when the RERA record and at least two other sources returned data; Medium when one agent failed; Low when the RERA record is missing. A low-confidence report says so prominently instead of showing a clean-looking number.

**Evidence rule:** every deduction stores `source_url`, `retrieved_at`, `tinyfish_tool`, `run_id` and a short excerpt. The UI shows the deduction, its points and a link to the evidence.

## 10. Data model

```
Audit(id, input_type, listing_url, project_name, builder, city, state,
      status, created_at, trust_score, risk_band, confidence)

Claims(audit_id, possession_date, rera_id, carpet_area, price, raw_excerpt)

ReraRecord(audit_id, rera_id, promoter, status, original_completion,
           revised_completion, extensions_count, complaints_count,
           project_url, run_id)

BuilderProject(audit_id, name, rera_id, extensions_count,
               complaints_count, status, run_id)

Finding(id, audit_id, type, severity, title, points, source_url,
        retrieved_at, excerpt, run_id)

RunLog(id, audit_id, tool, input_summary, target_url, run_id, status,
       started_at, duration_ms, raw_output_json, is_cached)

Watch(id, audit_id, monitor_id, type, target, cron, baseline_json,
      last_checked_at, last_change_json)
```

## 11. API specification

| Method and path | Purpose |
| --- | --- |
| `POST /api/audits` | Create an audit from a listing URL or manual fields |
| `GET /api/audits/{id}` | Full report JSON (claims, record, findings, score) |
| `GET /api/audits/{id}/events` | SSE stream of agent progress |
| `GET /api/audits/{id}/runs` | Agent Console data (RunLog rows) |
| `POST /api/audits/{id}/select-candidate` | Choose a project when RERA returns several |
| `POST /api/watches` | Create monitors for an audited project |
| `GET /api/watches` | List watched projects and detected changes |
| `POST /api/webhooks/tinyfish` | Receive monitor results and diff against baseline |

## 12. TinyFish call catalogue

This table is what to read before demoing. For every call, know what is sent and what comes back.

| Feature | Tool | Key inputs | Returns (what you store and show) |
| --- | --- | --- | --- |
| F2 listing | `fetch_content` | `urls`, `format: markdown`, `purpose` | Page text, title, per-URL errors |
| F2 fallback | `run_web_automation` | `url`, `goal`, `browser_profile: stealth` | Run result text or structured JSON |
| F3 RERA | `run_web_automation` | `url`, `goal`, `output_schema`, `agent_config`, `capture_config` | Structured JSON matching the schema, run ID, status |
| F5 fan-out | `run_web_automation_async`, `batch_status`, `wait_for_run` | One goal per project, up to 8 IDs per status call | Per-run status and result |
| F6 news | `search`, then `fetch_content` | `domain_type: news`, `after_date`, `location`, `purpose` | Ranked results (title, URL, snippet, date), then article text |
| F7 forums | `search`, then `fetch_content` | `include_domains`, `purpose` | Ranked threads, then thread text |
| F11 watch | `create_monitor` | `type`, `config.url` or `config.query`, `schedule_cron`, `purpose`, `webhook_url` | Monitor ID and baseline result; later webhook payloads |

Exact request and response field names come from the TinyFish API documentation and your account. Before you rely on any field in code, log one real response and read it. The MCP tool schemas are a guide, but the REST payloads may differ in naming.

## 13. Frontend pages

| Page | Content |
| --- | --- |
| Home | Input (URL or manual form), short explanation, example audit |
| Report | Claims card, RERA record card, contradictions list, builder track record table, news and resident panels, Trust Score with itemised evidence |
| Agent Console | Per-run cards: tool, goal, URL, run ID, status, duration, raw JSON, screenshot, LIVE or CACHED badge |
| Watchlist | Monitored projects, schedule, last check, changes detected |
| Disclaimer footer | "Informational only. Not legal advice. Verify on the official RERA portal." |

Design notes: dark theme to match the deck, severity colours with text labels (not colour alone), and a visible progress state per agent.

## 14. Reliability, honesty and edge cases

- **Portal failures:** government sites can be slow, change layout or show captchas. Cap run time, show the failure in the console and lower the confidence.
- **Cached fallback:** save one real, successful audit as a cached sample. The UI must label it **CACHED** with the original retrieval time. Never present cached data as live.
- **No dummy data:** if a stage fails, the report shows "not retrieved" for that stage.
- **Ambiguous matches:** never auto-pick between near-identical projects; show candidates.
- **LLM guardrails:** extraction prompts return `null` instead of guessing, and every classified finding must include a supporting excerpt. Findings without evidence are discarded.
- **Language of findings:** use "reported by" and "per RERA record", not accusations.
- **Duplicate names:** match on RERA ID when available.

## 15. Legal, ethical and privacy notes

- Only publicly available pages are accessed. Respect each site's terms and avoid aggressive request rates; the fan-out is capped.
- No login-gated or personal data is collected. Watchlist stores only project identifiers.
- Reports carry a disclaimer, and forum content is shown as short excerpts with links back to the source.
- The TinyFish key is never exposed to the client.

## 16. Build plan for the remaining time

Based on 3 to 4 hours before the 4:30 PM deadline. Cut from the bottom if you fall behind.

| Block | Work | Priority |
| --- | --- | --- |
| 0:00 to 0:30 | Run one real RERA automation manually with your key. Log the raw response. Decide the schema from what actually comes back. | P0 |
| 0:30 to 1:30 | FastAPI skeleton, TinyFish wrapper with `RunLog`, F3 RERA agent, F4 diff, F9 score | P0 |
| 1:30 to 2:15 | F6 news and F7 resident agents, F2 listing extraction (with manual form fallback), SSE events | P0 |
| 2:15 to 3:15 | Frontend: home, report page, Trust Score, Agent Console | P0 |
| 3:15 to 3:40 | F11 one monitor with baseline display | P0 |
| 3:40 to 4:00 | F5 builder fan-out (if RERA agent is stable) | P1 |
| Last 30 min | Save cached sample, record backup demo video, rehearse pitch, fix deck | P0 |

**Cut order if late:** F12 share link, then F10 disambiguation UI, then F5 fan-out, then the second monitor.

## 17. Demo script (4 minutes)

1. **Problem (30 sec):** show a listing and say the possession date it claims.
2. **Live audit (90 sec):** paste the URL, watch panels fill in via SSE.
3. **The reveal (45 sec):** open the contradiction card: listing date vs. RERA record, with the extension count.
4. **Proof it is real (45 sec):** open the Agent Console and show a run ID, the goal sent, the raw JSON and a screenshot.
5. **After the audit (30 sec):** click Watch this project and show the monitor baseline.

## 18. Mapping to judging criteria

| Criterion (weight) | How TrueEstate addresses it |
| --- | --- |
| Use of TinyFish (35%) | Four capabilities used for distinct jobs: portal automation with structured output, parallel runs, scoped search and fetch, scheduled monitors |
| Problem relevance and impact (25%) | Real homebuyer risk, grounded in RERA data and buyer survey figures |
| Technical execution and working demo (25%) | Parallel orchestration, deterministic diff and scoring, Agent Console showing real runs |
| Presentation (15%) | Clear claim-vs-record story, visible evidence, rehearsed 4-minute demo |

## 19. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| RERA portal slow, changed or captcha-protected | Test it first, cap runtime, cached labelled fallback, honest failure display |
| Listing sites block fetch | Stealth profile fallback, then manual form |
| Run time too long for live demo | Cap fan-out at 5 projects, stream partial results |
| LLM misclassifies forum text | Require excerpts and two sources, show "unverified" otherwise |
| Credits exhausted | Cache, limit retries, avoid deep research runs |
| Judges see it as "just research" | Lead with contradiction detection, fan-out and monitors; show the console |

## 20. Open items to verify before building

1. Exact structure of the Karnataka RERA search and project pages and whether a captcha appears.
2. The real response shape of `run_web_automation` with `output_schema` on your account.
3. Whether the async automation and monitor webhook features are enabled for your hackathon credits.
4. Which LLM and key you will use for extraction and classification.
5. One reference project with a known mismatch, to use as the demo case.

## 21. Future roadmap

- More states (MahaRERA and others) through per-state portal configs.
- Court records via Indian Kanoon for builder litigation.
- Document checks (approved plan and occupancy certificate lookups).
- Alert delivery by email or WhatsApp.
- Comparison view across several shortlisted projects.
