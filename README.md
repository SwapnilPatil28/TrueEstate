# TrueEstate

Evidence-linked property due diligence: compare listing claims with public RERA
records, inspect recent public news and community results, and optionally monitor
an official project page for changes. Reports include public neighborhood searches
for an entered or listing-extracted locality, including flooding/waterlogging,
drainage, power, water supply, traffic and other explicitly reported topics.
Reports are informational and are not legal advice or a guarantee of neighborhood
safety.

## Requirements

- Python 3.11 or newer
- Node.js 20.9 or newer
- A TinyFish API key for live browsing, search, fetch and monitors
- Optional OpenRouter API key for extracting listing claims and classifying fetched
  evidence excerpts. Without it, listing extraction uses TinyFish browser
  automation, and unclassified news/community results are withheld from the
  report. The default model is OpenRouter's `openrouter/free` router; override
  `OPENROUTER_MODEL` if you want to pin a specific model.

## Run locally (Windows PowerShell)

In the repository root, open two terminals.

Backend:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
# Open .env in an editor and add your API keys
python -m uvicorn app.main:app --reload --port 8000
```

In `backend\.env`, set `TINYFISH_API_KEY` to your TinyFish key. Set
`OPENROUTER_API_KEY` to your OpenRouter key if you want AI extraction and source
classification; `OPENROUTER_MODEL=openrouter/free` is the default. Entering a
locality (for example, Whitefield) improves the targeted neighborhood searches.
For listing-link audits, the extractor also captures the full advertised property
address and the named neighborhood/suburb; if no locality name is identified, the
address is used to derive a neighborhood search term (excluding unit numbers and
the city when identifiable), with the full address retained for source matching.
When the initial listing extraction misses a required project, builder, or city,
the audit also opens the listing in TinyFish browser automation before stopping.
If the page still does not confirm all required details, the report preserves the
partial extraction and lets you enter the missing values to resume that same audit.
Searches cover project news, Reddit, broader public web discussions, local news,
and other locality-focused web sources. OpenRouter summarizes fetched pages, but
the report shows a result only when its exact supporting quote and project/locality
identity are present in the fetched source. Summaries are not independent
fact-checks; one resident thread is a mention, not a confirmed issue. Keep the
other local defaults as-is. `PUBLIC_BASE_URL` and `WEBHOOK_SECRET` are only
needed for scheduled monitors deployed at a public HTTPS address. Do not share
or commit your `.env` file.

The builder portfolio section uses a separate TinyFish Karnataka RERA search,
verifies up to five other project detail pages, and searches for matching public
references for each verified project. Public reference links are shown separately
from official RERA records; no matching links does not mean no public discussion
exists.
The primary RERA lookup submits exactly one portal field per attempt: the
registration ID when available, otherwise the project name. If it cannot verify
a match, it searches and fetches web results using the listing's project, builder,
locality, and address, then retries the portal lookup twice. Search results only
help identify the query; the official RERA record must still be verified on the
portal before it affects the report.

Frontend:

```powershell
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). If API keys are not set,
manual project entry still works, while live retrieval attempts are explicitly
marked as unavailable in the report's Agent Console. Never put API keys in the
frontend environment or commit them to the repository.
Partial or failed reports include a **Retry research** action. It clears stale
retrieval results and reruns the live RERA, news, locality and builder checks;
an unknown score remains unknown until an official RERA record is verified.
Reports that need project details can be resumed from the report page after you
fill in the missing project name, builder, or city.

## TinyFish monitors

To create a scheduled monitor, deploy the backend at a public HTTPS URL and set
`PUBLIC_BASE_URL` to that URL plus a strong `WEBHOOK_SECRET`. The secret is
passed to TinyFish as a webhook query token and is not included in stored run
logs. The backend stores audits, run logs, progress events and watch entries
in SQLite.

The current portal adapter targets Karnataka RERA. Other state selections are
not enabled until their portal workflows are configured. A result without a
valid retrieval flag, registration ID and official project URL remains unknown;
an unsuccessful search is not treated as proof that a project is unregistered.

## Validation

```powershell
cd backend
python -m pytest
```

```powershell
cd frontend
npm run build
npm run lint
```
