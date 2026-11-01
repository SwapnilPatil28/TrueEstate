# TrueEstate

Evidence-linked property due diligence: compare listing claims with public RERA
records, inspect recent public news and community results, and optionally monitor
an official project page for changes. Reports are informational and are not legal
advice.

## Requirements

- Python 3.11 or newer
- Node.js 20.9 or newer
- A TinyFish API key for live browsing, search, fetch and monitors
- Optional OpenRouter API key for extracting listing claims and classifying fetched
  evidence excerpts. Without it, listing extraction uses TinyFish browser
  automation, and news/community results remain unclassified and do not affect
  the score. The default model is OpenRouter's `openrouter/free` router; override
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
classification; `OPENROUTER_MODEL=openrouter/free` is the default. Keep the
other local defaults as-is. `PUBLIC_BASE_URL` and `WEBHOOK_SECRET` are only
needed for scheduled monitors deployed at a public HTTPS address. Do not share
or commit your `.env` file.

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

## TinyFish monitors

To create a scheduled monitor, deploy the backend at a public HTTPS URL and set
`PUBLIC_BASE_URL` to that URL plus a strong `WEBHOOK_SECRET`. The secret is
passed to TinyFish as a webhook query token and is not included in stored run
logs. The backend stores audits, run logs, progress events and watch entries
in SQLite.

The current portal adapter targets Karnataka RERA. Other state selections are
not enabled until their portal workflows are configured.

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
