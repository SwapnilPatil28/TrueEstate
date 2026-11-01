import asyncio
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlsplit
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from app import database
from app.schemas import AuditCreate, CandidateSelection, WatchCreate, WebhookPayload
from app.scoring import calculate_score, compare_claims, evidence_findings


TINYFISH_API_KEY = os.getenv("TINYFISH_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
KARNATAKA_RERA_URL = "https://rera.karnataka.gov.in/"
KARNATAKA_RERA_HOST = "rera.karnataka.gov.in"
DISCLAIMER = (
    "Informational only. Not legal advice. Verify details on the official RERA portal."
)

RERA_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "rera_id": {"type": "string", "nullable": True},
        "project_name": {"type": "string", "nullable": True},
        "promoter": {"type": "string", "nullable": True},
        "status": {"type": "string", "nullable": True},
        "original_completion_date": {"type": "string", "nullable": True},
        "revised_completion_date": {"type": "string", "nullable": True},
        "extensions_count": {"type": "integer", "nullable": True},
        "complaints_count": {"type": "integer", "nullable": True},
        "project_url": {"type": "string", "nullable": True},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "project_name": {"type": "string", "nullable": True},
                    "promoter": {"type": "string", "nullable": True},
                    "rera_id": {"type": "string", "nullable": True},
                    "status": {"type": "string", "nullable": True},
                    "original_completion_date": {"type": "string", "nullable": True},
                    "revised_completion_date": {"type": "string", "nullable": True},
                    "extensions_count": {"type": "integer", "nullable": True},
                    "complaints_count": {"type": "integer", "nullable": True},
                    "project_url": {"type": "string", "nullable": True},
                },
            },
        },
    },
    "required": ["found"],
}

@asynccontextmanager
async def lifespan(_: FastAPI):
    database.initialize()
    yield


app = FastAPI(title="TrueEstate API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        os.getenv("FRONTEND_ORIGIN", "http://localhost:3000"),
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class IntegrationError(Exception):
    pass


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        for secret in (TINYFISH_API_KEY, OPENROUTER_API_KEY, WEBHOOK_SECRET):
            if len(secret) >= 4:
                value = value.replace(secret, "[redacted]")
        return value
    return value


def is_official_rera_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.hostname.casefold() == KARNATAKA_RERA_HOST
        and parsed.username is None
        and parsed.password is None
    )


async def openrouter_chat_json(
    system_prompt: str, user_content: str, timeout_seconds: int = 60
) -> dict[str, Any]:
    if not OPENROUTER_API_KEY:
        raise IntegrationError("OPENROUTER_API_KEY is not configured.")
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "HTTP-Referer": "https://trueestate.app",
                "X-Title": "TrueEstate",
            },
            json={
                "model": OPENROUTER_MODEL,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            },
        )
        response.raise_for_status()
        completion = response.json()
    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenRouter returned no completion.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ValueError("OpenRouter returned no JSON content.")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("OpenRouter response must be a JSON object.")
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_audit(audit_id: str, **fields: Any) -> dict[str, Any]:
    audit = database.load_audit(audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found.")
    audit.update(fields)
    audit["updated_at"] = now_iso()
    database.save_audit(audit)
    return audit


async def emit(audit_id: str, stage: str, status: str, message: str) -> None:
    event = {
        "stage": stage,
        "status": status,
        "message": message,
        "created_at": now_iso(),
    }
    database.add_event(audit_id, event)
    audit = database.load_audit(audit_id)
    if audit:
        audit["progress"].append(event)
        audit["updated_at"] = event["created_at"]
        database.save_audit(audit)


async def tinyfish_call(
    audit_id: str,
    tool: str,
    input_summary: str,
    target_url: str | None,
    request: Callable[[httpx.AsyncClient], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    run_log_id = str(uuid.uuid4())
    started_at = now_iso()
    started = time.monotonic()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO runs (id, audit_id, tool, input_summary, target_url, status, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_log_id, audit_id, tool, input_summary, target_url, "RUNNING", started_at),
        )

    status = "FAILED"
    run_id = None
    raw: dict[str, Any]
    try:
        if not TINYFISH_API_KEY:
            raise IntegrationError("TINYFISH_API_KEY is not configured.")
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(180, connect=15),
            headers={"X-API-Key": TINYFISH_API_KEY},
        ) as client:
            raw = await request(client)
        run_id = raw.get("run_id") or raw.get("id")
        status_value = str(raw.get("status", "COMPLETED")).upper()
        if status_value in {"FAILED", "ERROR", "CANCELLED"} or raw.get("error"):
            error = raw.get("error") or status_value
            raise IntegrationError(f"TinyFish run failed: {error}")
        status = "COMPLETED"
    except IntegrationError as error:
        raw = {"error": str(error)}
    except httpx.HTTPStatusError as error:
        status_code = error.response.status_code
        detail = error.response.text[:1000]
        raw = {"error": f"TinyFish returned HTTP {status_code}: {detail}"}
    except httpx.RequestError as error:
        raw = {"error": f"TinyFish request failed: {type(error).__name__}."}
    except (ValueError, json.JSONDecodeError) as error:
        raw = {"error": f"TinyFish returned invalid JSON: {error}"}

    duration_ms = int((time.monotonic() - started) * 1000)
    if status == "COMPLETED":
        raw["trueestate_trace_id"] = run_log_id
    with database.connect() as connection:
        connection.execute(
            "UPDATE runs SET run_id = ?, status = ?, duration_ms = ?, raw_output_json = ? WHERE id = ?",
            (
                run_id,
                status,
                duration_ms,
                json.dumps(redact_secrets(raw), ensure_ascii=False),
                run_log_id,
            ),
        )
    if status != "COMPLETED":
        raise IntegrationError(raw["error"])
    return raw


async def agent_request(
    audit_id: str,
    goal: str,
    url: str,
    output_schema: dict[str, Any] | None = None,
    browser_profile: str = "lite",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "url": url,
        "goal": goal,
        "browser_profile": browser_profile,
        "agent_config": {"max_duration_seconds": 150},
    }
    if output_schema:
        body["output_schema"] = output_schema

    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.post(
            "https://agent.tinyfish.ai/v1/automation/run", json=body
        )
        response.raise_for_status()
        return response.json()

    result = await tinyfish_call(
        audit_id, "run_web_automation", goal, url, request
    )
    payload = result.get("result")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {"text": payload}
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return {
        **(payload if isinstance(payload, dict) else {"result": payload}),
        "run_id": result.get("run_id"),
        "trace_id": result.get("trueestate_trace_id"),
    }


async def fetch_content(
    audit_id: str, urls: list[str], purpose: str
) -> dict[str, Any]:
    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.post(
            "https://api.fetch.tinyfish.ai",
            json={"urls": urls, "format": "markdown", "purpose": purpose},
        )
        response.raise_for_status()
        return response.json()

    return await tinyfish_call(
        audit_id, "fetch_content", purpose, ", ".join(urls), request
    )


async def search_web(
    audit_id: str,
    query: str,
    domain_type: str = "web",
    include_domains: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "query": query,
        "location": "IN",
        "language": "en",
        "domain_type": domain_type,
        "purpose": "Public due diligence for a real-estate project audit.",
        "after_date": (datetime.now(timezone.utc) - timedelta(days=730)).date().isoformat(),
    }
    if include_domains:
        params["include_domains"] = include_domains

    async def request(client: httpx.AsyncClient) -> dict[str, Any]:
        response = await client.get(
            "https://api.search.tinyfish.ai", params=params
        )
        response.raise_for_status()
        return response.json()

    return await tinyfish_call(
        audit_id,
        "search",
        query,
        "https://api.search.tinyfish.ai",
        request,
    )


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("results")
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def _fetch_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("results") or payload.get("data") or []
    if isinstance(items, dict):
        items = [items]
    return [item for item in items if isinstance(item, dict)]


async def classify_sources(
    news: list[dict[str, Any]], residents: list[dict[str, Any]]
) -> None:
    sources = [
        {
            "url": item["url"],
            "title": item.get("title"),
            "source_type": source_type,
            "text": item["_source_text"][:5000],
        }
        for source_type, group in (("news", news), ("resident_forum", residents))
        for item in group
        if item.get("url") and item.get("_source_text")
    ]
    if not sources:
        return
    answer = await openrouter_chat_json(
        (
            "Classify each supplied public source for a real-estate due-diligence "
            "report. Do not infer facts. Set relevant only if the text identifies the "
            "project, builder, or stated locality. For news, mark negative only for "
            "explicit negative coverage about that builder/project. For resident posts, "
            "choose exactly one issue_category from waterlogging, power, water_supply, "
            "structural, traffic, other, or null. Return one JSON object with an items "
            "array containing url, source_type, relevant (boolean), negative (boolean), "
            "issue_category (string or null), and excerpt (a short exact quote copied "
            "verbatim from the source; empty string if no relevant supporting quote exists)."
        ),
        json.dumps(sources, ensure_ascii=False),
    )
    if not isinstance(answer.get("items"), list):
        raise ValueError("AI source classification did not return an items array.")

    item_by_url = {
        (item["url"], "news"): item for item in news if item.get("url")
    }
    item_by_url.update(
        {(item["url"], "resident_forum"): item for item in residents if item.get("url")}
    )
    for classification in answer.get("items", []):
        if not isinstance(classification, dict):
            continue
        source_url = classification.get("url")
        source_type = classification.get("source_type")
        if not isinstance(source_url, str) or not isinstance(source_type, str):
            continue
        item = item_by_url.get(
            (source_url, source_type)
        )
        if not item:
            continue
        excerpt = str(classification.get("excerpt") or "").strip()
        source_text = re.sub(r"\s+", " ", item.get("_source_text", "")).strip()
        normalized_source = source_text.casefold()
        normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip().casefold()
        verified = bool(normalized_excerpt and normalized_excerpt in normalized_source)
        category = classification.get("issue_category")
        allowed_categories = {
            "waterlogging",
            "power",
            "water_supply",
            "structural",
            "traffic",
            "other",
        }
        item["classification"] = {
            "relevant": classification.get("relevant") is True,
            "negative": classification.get("negative") is True,
            "issue_category": (
                category
                if isinstance(category, str) and category in allowed_categories
                else None
            ),
            "excerpt": excerpt if verified else "",
            "verified": verified,
        }


async def extract_listing(audit_id: str, listing_url: str) -> dict[str, Any]:
    schema = {
        "type": "object",
        "properties": {
            "project_name": {"type": "string", "nullable": True},
            "builder": {"type": "string", "nullable": True},
            "locality": {"type": "string", "nullable": True},
            "city": {"type": "string", "nullable": True},
            "state": {"type": "string", "nullable": True},
            "possession_date": {"type": "string", "nullable": True},
            "rera_id": {"type": "string", "nullable": True},
            "carpet_area": {"type": "string", "nullable": True},
            "price": {"type": "string", "nullable": True},
            "raw_excerpt": {"type": "string", "nullable": True},
        },
    }
    if OPENROUTER_API_KEY and TINYFISH_API_KEY:
        try:
            fetched = await fetch_content(
                audit_id,
                [listing_url],
                "Extract factual listing claims for a property due diligence audit.",
            )
            pages = _fetch_items(fetched)
            text = "\n".join(str(page.get("text") or "") for page in pages)
            if text.strip():
                return await llm_extract_claims(audit_id, text)
        except (IntegrationError, httpx.HTTPError, ValueError, KeyError, IndexError):
            pass

    goal = (
        "Read this property listing and extract only claims explicitly shown on the page. "
        "Return the requested JSON schema. Use null for every missing field; never infer or guess."
    )
    return await agent_request(
        audit_id, goal, listing_url, schema, browser_profile="stealth"
    )


async def llm_extract_claims(audit_id: str, page_text: str) -> dict[str, Any]:
    del audit_id
    schema = {
        "project_name": "string or null",
        "builder": "string or null",
        "locality": "string or null",
        "city": "string or null",
        "state": "string or null",
        "possession_date": "string or null",
        "rera_id": "string or null",
        "carpet_area": "string or null",
        "price": "string or null",
        "raw_excerpt": "string or null",
    }
    parsed = await openrouter_chat_json(
        (
            "Extract only facts explicitly present in the supplied listing text. "
            "Never guess; use null for missing values. Return JSON matching "
            + json.dumps(schema)
        ),
        page_text[:18000],
        timeout_seconds=45,
    )
    expected_fields = {
        "project_name",
        "builder",
        "locality",
        "city",
        "state",
        "possession_date",
        "rera_id",
        "carpet_area",
        "price",
        "raw_excerpt",
    }
    for field in expected_fields:
        value = parsed.get(field)
        if value is not None and not isinstance(value, str):
            parsed[field] = None
    parsed["raw_excerpt"] = parsed.get("raw_excerpt") or ""
    return parsed


async def gather_evidence(
    audit_id: str, project_name: str, builder: str, city: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool, bool, str | None]:
    news_query = f'"{builder}" "{project_name}" delay OR complaint OR RERA'
    resident_query = f'"{project_name}" {city} waterlogging OR seepage OR power cuts'
    news_result, resident_result = await asyncio.gather(
        search_web(audit_id, news_query, "news"),
        search_web(audit_id, resident_query, "web", "reddit.com"),
        return_exceptions=True,
    )
    failed = 0
    news = []
    residents = []
    if isinstance(news_result, Exception):
        failed += 1
    else:
        news = _results(news_result)[:5]
        for item in news:
            item["trace_id"] = news_result.get("trueestate_trace_id")
    if isinstance(resident_result, Exception):
        failed += 1
    else:
        residents = _results(resident_result)[:5]
        for item in residents:
            item["trace_id"] = resident_result.get("trueestate_trace_id")
    news_retrieved = not isinstance(news_result, Exception)
    residents_retrieved = not isinstance(resident_result, Exception)

    unique_urls = list(
        dict.fromkeys(
            str(item["url"])
            for item in news + residents
            if item.get("url", "").startswith(("http://", "https://"))
        )
    )[:8]
    if unique_urls:
        try:
            fetched = await fetch_content(
                audit_id,
                unique_urls,
                "Retrieve source text for an evidence-linked property due diligence report.",
            )
            fetch_trace_id = fetched.get("trueestate_trace_id")
            texts = {
                item.get("url"): item.get("text")
                for item in _fetch_items(fetched)
                if item.get("url")
            }
            for item in news + residents:
                text = texts.get(item.get("url"))
                if text:
                    item["_source_text"] = str(text)
                    item["excerpt"] = re.sub(r"\s+", " ", str(text)).strip()[:420]
                    item["trace_id"] = fetch_trace_id or item.get("trace_id")
                    item["tinyfish_tool"] = "fetch_content"
                else:
                    item["excerpt"] = item.get("snippet", "")
        except IntegrationError:
            failed += 1
            for item in news + residents:
                item["excerpt"] = item.get("snippet", "")
    else:
        for item in news + residents:
            item["excerpt"] = item.get("snippet", "")
    classification_error = None
    if OPENROUTER_API_KEY:
        try:
            await classify_sources(news, residents)
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
            classification_error = f"{type(error).__name__}: {error}"
    for item in news + residents:
        item.pop("_source_text", None)
    return (
        news,
        residents,
        failed,
        news_retrieved,
        residents_retrieved,
        classification_error,
    )


async def collect_builder_projects(
    audit_id: str, builder: str, city: str, primary_rera_id: str | None
) -> list[dict[str, Any]]:
    schema = {
        "type": "object",
        "properties": {
            "projects": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "properties": {
                        "project_name": {"type": "string", "nullable": True},
                        "promoter": {"type": "string", "nullable": True},
                        "rera_id": {"type": "string", "nullable": True},
                        "project_url": {"type": "string", "nullable": True},
                    },
                },
            }
        },
        "required": ["projects"],
    }
    discovery = await agent_request(
        audit_id,
        (
            f"Search the Karnataka RERA portal for up to five other registered projects "
            f"by promoter {builder}, including projects around {city}. Exclude the "
            f"current project with RERA ID {primary_rera_id or 'not supplied'}. For each "
            "project return its exact name, promoter, RERA ID and official project detail "
            "URL. Only include projects with an official detail URL; do not guess."
        ),
        KARNATAKA_RERA_URL,
        schema,
    )
    projects = discovery.get("projects")
    if not isinstance(projects, list):
        return []
    primary_id = re.sub(r"[^a-z0-9]", "", (primary_rera_id or "").lower())
    unique: dict[str, dict[str, Any]] = {}
    for project in projects:
        if not isinstance(project, dict):
            continue
        url = project.get("project_url")
        if not is_official_rera_url(url):
            continue
        rera_id = project.get("rera_id")
        normalized_id = re.sub(r"[^a-z0-9]", "", str(rera_id or "").lower())
        if primary_id and normalized_id == primary_id:
            continue
        unique[normalized_id or url] = project

    async def inspect(project: dict[str, Any]) -> dict[str, Any] | None:
        try:
            detail = await agent_request(
                audit_id,
                (
                    "Read this Karnataka RERA project detail page. Return its RERA ID, "
                    "project name, promoter, current registration status, original and "
                    "revised completion dates in YYYY-MM-DD format, extension count, "
                    "complaint count and canonical project URL. Use null for fields "
                    "that are not shown."
                ),
                project["project_url"],
                RERA_SCHEMA,
            )
        except IntegrationError:
            return None
        if not detail.get("found"):
            return None
        return {
            **project,
            **detail,
            "project_url": detail.get("project_url") or project["project_url"],
            "retrieved_at": now_iso(),
        }

    inspected = await asyncio.gather(
        *(inspect(project) for project in list(unique.values())[:5])
    )
    return [project for project in inspected if project is not None]


async def run_audit(audit_id: str) -> None:
    audit = database.load_audit(audit_id)
    if not audit:
        return
    try:
        await emit(audit_id, "extract", "started", "Preparing listing claims.")
        claims = audit["claims"]
        listing_url = audit.get("listing_url")
        if listing_url:
            if not claims.get("project_name") or not claims.get("builder"):
                try:
                    extracted = await extract_listing(audit_id, listing_url)
                    claims = {
                        **claims,
                        **{
                            key: value
                            for key, value in extracted.items()
                            if value is not None
                        },
                    }
                except (IntegrationError, httpx.HTTPError, ValueError, KeyError) as error:
                    await emit(
                        audit_id,
                        "extract",
                        "failed",
                        f"Listing extraction failed: {error}. Add project details manually to retry.",
                    )
            audit = _update_audit(audit_id, claims=claims)
        if claims.get("project_name") and claims.get("builder"):
            await emit(audit_id, "extract", "completed", "Listing claims are ready.")

        project = claims.get("project_name") or audit.get("project_name")
        builder = claims.get("builder") or audit.get("builder")
        city = claims.get("city") or audit.get("city")
        state = claims.get("state") or audit.get("state") or "Karnataka"
        if str(state).strip().casefold() != "karnataka":
            await emit(
                audit_id,
                "extract",
                "failed",
                f"Live RERA audits are not configured for {state}.",
            )
            await emit(
                audit_id,
                "rera",
                "failed",
                "Only Karnataka RERA is supported; no portal lookup was attempted.",
            )
            await emit(
                audit_id,
                "audit",
                "completed",
                "Audit stopped because this state is not supported yet.",
            )
            _update_audit(
                audit_id,
                status="unsupported",
                project_name=project,
                builder=builder,
                city=city,
                state=state,
                score=calculate_score(None, [], failed_agents=1),
            )
            return
        if not all((project, builder, city)):
            _update_audit(audit_id, status="needs_input", claims=claims)
            await emit(
                audit_id,
                "extract",
                "failed",
                "Project name, builder and city are required before the RERA search.",
            )
            await emit(audit_id, "audit", "completed", "Audit needs more project details.")
            return

        _update_audit(
            audit_id,
            project_name=project,
            builder=builder,
            city=city,
            state=state,
            status="running",
            claims=claims,
        )
        await emit(audit_id, "rera", "started", f"Searching {state} RERA records.")
        await emit(audit_id, "builder", "started", "Checking the builder's other RERA projects.")
        await emit(audit_id, "news", "started", "Searching recent local news.")
        await emit(audit_id, "residents", "started", "Searching public resident forums.")
        rera_goal = (
            f"Search the Karnataka RERA portal for the project {project}, by promoter "
            f"{builder}, in {city}. Open the best matching project detail page. Return "
            "the official registration status, RERA ID, promoter, original and revised "
            "completion dates, extension count, registered complaint count and project "
            "page URL. Return dates in YYYY-MM-DD format. If multiple plausible projects "
            "match, return up to three candidates with the same fields for each candidate "
            "and do not choose between ambiguous results. Do not invent missing fields."
        )
        rera_task = agent_request(
            audit_id,
            rera_goal,
            KARNATAKA_RERA_URL,
            RERA_SCHEMA,
        )
        evidence_task = gather_evidence(
            audit_id, str(project), str(builder), str(city)
        )
        builder_task = collect_builder_projects(
            audit_id, str(builder), str(city), claims.get("rera_id")
        )
        rera_result, evidence_result, builder_result = await asyncio.gather(
            rera_task, evidence_task, builder_task, return_exceptions=True
        )

        record: dict[str, Any] | None = None
        failures = 0
        if isinstance(rera_result, Exception):
            failures += 1
            await emit(
                audit_id,
                "rera",
                "failed",
                f"RERA lookup failed: {rera_result}",
            )
        else:
            record = {
                **rera_result,
                "retrieved_at": now_iso(),
            }
            if record.get("found") and not is_official_rera_url(
                record.get("project_url")
            ):
                record["found"] = False
                record["source_error"] = (
                    "The result did not include a verified Karnataka RERA project URL."
                )
            candidates = record.get("candidates") or []
            if len(candidates) > 1:
                record["ambiguous"] = True
                record["found"] = False
                record["project_url"] = None
            await emit(audit_id, "rera", "completed", "RERA lookup finished.")

        if isinstance(evidence_result, Exception):
            failures += 1
            news, residents, evidence_failures = [], [], 2
            news_retrieved = False
            residents_retrieved = False
            classification_error = None
        else:
            (
                news,
                residents,
                evidence_failures,
                news_retrieved,
                residents_retrieved,
                classification_error,
            ) = evidence_result
            failures += evidence_failures
        if isinstance(builder_result, Exception):
            builder_projects = []
            failures += 1
            await emit(
                audit_id,
                "builder",
                "failed",
                f"Builder portfolio lookup failed: {builder_result}",
            )
        else:
            builder_projects = builder_result
            primary_id = re.sub(
                r"[^a-z0-9]",
                "",
                str((record or {}).get("rera_id") or claims.get("rera_id") or "").lower(),
            )
            builder_projects = [
                item
                for item in builder_projects
                if not primary_id
                or re.sub(r"[^a-z0-9]", "", str(item.get("rera_id") or "").lower())
                != primary_id
            ]
            await emit(
                audit_id,
                "builder",
                "completed",
                f"Inspected {len(builder_projects)} other builder project(s).",
            )
        if not OPENROUTER_API_KEY:
            await emit(
                audit_id,
                "classification",
                "skipped",
                "No OPENROUTER_API_KEY configured; source results remain unclassified and do not affect the score.",
            )
        elif classification_error:
            failures += 1
            await emit(
                audit_id,
                "classification",
                "failed",
                f"Source classification failed: {classification_error}. Results remain unclassified.",
            )
        else:
            await emit(
                audit_id,
                "classification",
                "completed",
                "Source classifications were accepted only when their quoted excerpts matched fetched text.",
            )
        await emit(
            audit_id,
            "news",
            "completed" if news_retrieved else "failed",
            f"{len(news)} news result(s) retrieved; unverified results do not affect the score.",
        )
        await emit(
            audit_id,
            "residents",
            "completed" if residents_retrieved else "failed",
            f"{len(residents)} community result(s) retrieved; unverified mentions do not affect the score.",
        )

        retrieved_at = now_iso()
        findings = compare_claims(claims, record, retrieved_at)
        findings.extend(evidence_findings(news, residents, retrieved_at))
        score = calculate_score(
            record,
            findings,
            builder_projects=builder_projects,
            news_count=len(news),
            resident_count=len(residents),
            failed_agents=failures,
        )
        status = "completed" if record else "partial"
        await emit(audit_id, "audit", "completed", "Audit report is ready.")
        _update_audit(
            audit_id,
            status=status,
            claims=claims,
            rera_record=record,
            findings=findings,
            news=news,
            residents=residents,
            builder_projects=builder_projects,
            score=score,
        )
    except Exception as error:
        _update_audit(audit_id, status="failed", error=str(error))
        await emit(audit_id, "audit", "failed", f"Audit failed: {error}")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "tinyfish_configured": bool(TINYFISH_API_KEY),
        "llm_configured": bool(OPENROUTER_API_KEY),
        "llm_provider": "OpenRouter",
        "llm_model": OPENROUTER_MODEL,
    }


@app.post("/api/audits", status_code=202)
async def create_audit(payload: AuditCreate) -> dict[str, Any]:
    try:
        payload.validate_input()
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if payload.state.casefold() != "karnataka":
        raise HTTPException(
            status_code=422,
            detail="Live RERA audits are currently configured for Karnataka only.",
        )
    audit_id = str(uuid.uuid4())
    now = now_iso()
    claims = {
        "project_name": payload.project_name,
        "builder": payload.builder,
        "city": payload.city,
        "state": payload.state,
        "rera_id": payload.rera_id,
        "possession_date": (
            payload.possession_date.isoformat() if payload.possession_date else None
        ),
        "carpet_area": payload.carpet_area,
        "price": payload.price,
    }
    audit = {
        "id": audit_id,
        "listing_url": str(payload.listing_url) if payload.listing_url else None,
        "project_name": payload.project_name,
        "builder": payload.builder,
        "city": payload.city,
        "state": payload.state,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "claims": claims,
        "rera_record": None,
        "findings": [],
        "news": [],
        "residents": [],
        "builder_projects": [],
        "score": calculate_score(None, [], failed_agents=1),
        "progress": [],
        "disclaimer": DISCLAIMER,
    }
    database.save_audit(audit)
    await emit(audit_id, "audit", "started", "Audit queued.")
    asyncio.create_task(run_audit(audit_id))
    return {"id": audit_id, "status": "queued"}


@app.get("/api/audits/{audit_id}")
async def get_audit(audit_id: str) -> dict[str, Any]:
    audit = database.load_audit(audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found.")
    return audit


@app.get("/api/audits/{audit_id}/runs")
async def get_runs(audit_id: str) -> list[dict[str, Any]]:
    if not database.load_audit(audit_id):
        raise HTTPException(status_code=404, detail="Audit not found.")
    return database.list_runs(audit_id)


@app.get("/api/audits/{audit_id}/events")
async def audit_events(audit_id: str, request: Request) -> StreamingResponse:
    if not database.load_audit(audit_id):
        raise HTTPException(status_code=404, detail="Audit not found.")

    async def stream():
        last_event_id = request.headers.get("last-event-id", "0")
        last_id = int(last_event_id) if last_event_id.isdigit() else 0
        while True:
            if await request.is_disconnected():
                break
            rows = database.events_after(audit_id, last_id)
            for row in rows:
                last_id = row["id"]
                yield f"id: {last_id}\ndata: {row['event']}\n\n"
            audit = database.load_audit(audit_id)
            if audit and audit["status"] in {
                "completed",
                "partial",
                "failed",
                "needs_input",
                "unsupported",
            } and not rows:
                break
            if not rows:
                yield ": keep-alive\n\n"
                await asyncio.sleep(1)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/audits/{audit_id}/select-candidate")
async def select_candidate(
    audit_id: str, selection: CandidateSelection
) -> dict[str, Any]:
    audit = database.load_audit(audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found.")
    candidate = selection.candidate
    record = audit.get("rera_record") or {}
    candidates = record.get("candidates") or []
    if candidate not in candidates:
        raise HTTPException(status_code=400, detail="Candidate is not part of this audit.")
    project_url = candidate.get("project_url")
    if not is_official_rera_url(project_url):
        raise HTTPException(
            status_code=400,
            detail="This candidate has no verified Karnataka RERA project page.",
        )
    goal = (
        "Read this selected Karnataka RERA project detail page. Return its RERA ID, "
        "project name, promoter, current registration status, original and revised "
        "completion dates in YYYY-MM-DD format, extension count, complaint count and "
        "canonical project URL. Use null for fields that are not shown."
    )
    try:
        selected_record = await agent_request(
            audit_id,
            goal,
            project_url,
            RERA_SCHEMA,
        )
    except IntegrationError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    if not selected_record.get("found"):
        raise HTTPException(
            status_code=502,
            detail="TinyFish could not verify the selected RERA project page.",
        )
    record.update(candidate)
    record.update(selected_record)
    selected_url = selected_record.get("project_url") or project_url
    if not is_official_rera_url(selected_url):
        raise HTTPException(
            status_code=502,
            detail="The selected result did not resolve to an official Karnataka RERA page.",
        )
    record["project_url"] = selected_url
    record["found"] = True
    record["ambiguous"] = False
    record["retrieved_at"] = now_iso()
    findings = compare_claims(audit["claims"], record, now_iso())
    findings.extend(
        evidence_findings(audit.get("news", []), audit.get("residents", []), now_iso())
    )
    score = calculate_score(
        record,
        findings,
        builder_projects=audit.get("builder_projects", []),
        news_count=len(audit["news"]),
        resident_count=len(audit["residents"]),
    )
    _update_audit(
        audit_id,
        rera_record=record,
        findings=findings,
        score=score,
        status="completed",
    )
    return database.load_audit(audit_id) or {}


@app.post("/api/watches", status_code=201)
async def create_watch(payload: WatchCreate) -> dict[str, Any]:
    audit = database.load_audit(payload.audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found.")
    record = audit.get("rera_record") or {}
    target = record.get("project_url")
    if not is_official_rera_url(target):
        raise HTTPException(
            status_code=400,
            detail="A verified Karnataka RERA project URL is required to watch.",
        )
    if not PUBLIC_BASE_URL:
        raise HTTPException(
            status_code=503,
            detail="Set PUBLIC_BASE_URL to an HTTPS backend URL before creating monitors.",
        )
    if not PUBLIC_BASE_URL.startswith("https://"):
        raise HTTPException(status_code=503, detail="PUBLIC_BASE_URL must use HTTPS.")
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=503,
            detail="Set WEBHOOK_SECRET before creating a monitor.",
        )
    webhook_url = (
        f"{PUBLIC_BASE_URL}/api/webhooks/tinyfish"
        f"?secret={quote(WEBHOOK_SECRET, safe='')}"
    )
    monitor_specs = [
        {
            "type": "fetch",
            "name": f"{audit['project_name']} RERA changes",
            "config": {"url": target, "format": "markdown"},
            "schedule_cron": payload.schedule_cron,
            "purpose": "Notify if the public RERA project page changes.",
            "webhook_url": webhook_url,
            "target": target,
        },
        {
            "type": "search",
            "name": f"{audit['project_name']} news",
            "config": {
                "query": f'"{audit["builder"]}" "{audit["project_name"]}" news',
                "recency_minutes": 525600,
                "result_limit": 5,
            },
            "schedule_cron": payload.schedule_cron,
            "purpose": "Notify when recent public news about this project or builder appears.",
            "webhook_url": webhook_url,
            "target": f'"{audit["builder"]}" "{audit["project_name"]}" news',
        },
    ]

    async def create_monitor(spec: dict[str, Any]) -> dict[str, Any] | None:
        body = {key: value for key, value in spec.items() if key != "target"}

        async def request(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.post(
                "https://agent.tinyfish.ai/v1/monitors", json=body
            )
            response.raise_for_status()
            return response.json()

        try:
            result = await tinyfish_call(
                payload.audit_id,
                "create_monitor",
                f"Create scheduled {spec['type']} monitor for the audited project.",
                spec["target"],
                request,
            )
        except IntegrationError:
            return None
        monitor = result.get("monitor") or {}
        monitor_id = monitor.get("id")
        if not monitor_id:
            return None
        watch_id = str(uuid.uuid4())
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO watches (id, audit_id, monitor_id, type, target, cron, baseline_json, last_checked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    watch_id,
                    payload.audit_id,
                    monitor_id,
                    spec["type"],
                    spec["target"],
                    payload.schedule_cron,
                    json.dumps(result.get("run") or {}),
                    now_iso(),
                ),
            )
        return {
            "id": watch_id,
            "type": spec["type"],
            "monitor_id": monitor_id,
            "baseline": result.get("run"),
        }

    created = await asyncio.gather(
        *(create_monitor(spec) for spec in monitor_specs)
    )
    monitors = [monitor for monitor in created if monitor is not None]
    if not monitors:
        raise HTTPException(
            status_code=502,
            detail="TinyFish could not create either scheduled monitor. Check the Agent Console.",
        )
    return {"monitors": monitors, "partial": len(monitors) != len(monitor_specs)}


@app.get("/api/watches")
async def get_watches() -> list[dict[str, Any]]:
    return database.list_watches()


@app.post("/api/webhooks/tinyfish")
async def tinyfish_webhook(
    payload: WebhookPayload,
    secret: str | None = None,
) -> dict[str, str]:
    if not WEBHOOK_SECRET or not secret or not secrets.compare_digest(
        secret, WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")
    body = payload.model_dump(exclude_none=True)
    monitor = body.get("monitor")
    monitor_id = (
        body.get("monitor_id")
        or body.get("id")
        or (monitor.get("id") if isinstance(monitor, dict) else None)
    )
    current_result = body.get("run") or body.get("result") or body.get("results")
    if current_result is None:
        raise HTTPException(status_code=422, detail="Webhook contains no monitor result.")

    def snapshot(value: Any) -> Any:
        if isinstance(value, dict):
            if "run" in value:
                return snapshot(value["run"])
            if "results" in value:
                return snapshot(value["results"])
            ignored = {
                "id",
                "created_at",
                "updated_at",
                "latency_ms",
                "is_baseline",
                "monitor_id",
            }
            return {
                key: snapshot(item)
                for key, item in value.items()
                if key not in ignored
            }
        if isinstance(value, list):
            return [snapshot(item) for item in value]
        return value

    with database.connect() as connection:
        row = connection.execute(
            "SELECT id, baseline_json FROM watches WHERE monitor_id = ?",
            (monitor_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Monitor not found.")
        baseline = json.loads(row["baseline_json"]) if row["baseline_json"] else None
        changed = snapshot(current_result) != snapshot(baseline)
        connection.execute(
            "UPDATE watches SET last_checked_at = ?, "
            "last_change_json = CASE WHEN ? THEN ? ELSE last_change_json END, "
            "baseline_json = ? WHERE id = ?",
            (
                now_iso(),
                changed,
                json.dumps(body, ensure_ascii=False) if changed else None,
                json.dumps(current_result, ensure_ascii=False),
                row["id"],
            ),
        )
    return {"status": "accepted"}
