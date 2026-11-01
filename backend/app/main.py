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
from app.schemas import (
    AuditCreate,
    AuditRetry,
    CandidateSelection,
    WatchCreate,
    WebhookPayload,
)
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


def _contains_entity(text: str, entities: list[str]) -> bool:
    normalized_text = f" {re.sub(r'[^a-z0-9]+', ' ', text.casefold()).strip()} "
    return any(
        normalized_entity
        and f" {normalized_entity} " in normalized_text
        for normalized_entity in (
            re.sub(r"[^a-z0-9]+", " ", entity.casefold()).strip()
            for entity in entities
        )
    )


def _locality_entities(subject: dict[str, Any]) -> list[str]:
    locality = subject.get("locality")
    if isinstance(locality, str) and locality.strip():
        return [locality.strip()]
    city = str(subject.get("city") or "").strip().casefold()
    state = str(subject.get("state") or "").strip().casefold()
    address = subject.get("property_address")
    if not isinstance(address, str):
        return []
    entities: list[str] = []
    for part in address.split(","):
        part = re.sub(
            r"^(?:plot|flat|unit|door|house|apt|apartment|no\.?)\s*[\w/-]+\s*",
            "",
            part.strip(),
            flags=re.IGNORECASE,
        ).strip()
        if (
            not part
            or part.casefold() in {city, state}
            or len(re.sub(r"[^a-zA-Z]", "", part)) < 4
        ):
            continue
        entities.append(part)
        first_word = re.split(r"\W+", part)[0]
        if len(first_word) >= 5:
            entities.append(first_word)
    return entities


def _locality_search_name(
    locality: str | None, property_address: str | None, city: str
) -> str:
    if locality and locality.strip():
        return locality.strip()
    address_entities = _locality_entities(
        {"property_address": property_address, "city": city}
    )
    if len(address_entities) > 1:
        last_word = re.split(r"\W+", address_entities[-2])[0].casefold()
        if address_entities[-1].casefold() == last_word:
            return address_entities[-2]
    return address_entities[-1] if address_entities else property_address or city


def _rera_validation_error(result: dict[str, Any]) -> str | None:
    if type(result.get("found")) is not bool:
        return "RERA automation returned no valid found flag."
    candidates = result.get("candidates")
    if candidates is not None and not isinstance(candidates, list):
        return "RERA automation returned a malformed candidates list."
    if isinstance(candidates, list) and len(candidates) > 1:
        if all(
            isinstance(candidate, dict)
            and isinstance(candidate.get("rera_id"), str)
            and candidate["rera_id"].strip()
            and is_official_rera_url(candidate.get("project_url"))
            for candidate in candidates
        ):
            return None
        return "RERA automation returned candidates without verified official records."
    if result["found"] is False:
        return "No official project record was verified; non-registration is unknown."
    rera_id = result.get("rera_id")
    if not isinstance(rera_id, str) or not rera_id.strip():
        return "RERA automation did not return a registration ID."
    if not is_official_rera_url(result.get("project_url")):
        return "RERA automation did not return an official Karnataka RERA project URL."
    return None


def _normalize_match_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _rera_result_matches(
    result: dict[str, Any], project_name: str, rera_id: str | None
) -> bool:
    expected_id = _normalize_match_text(rera_id or "").replace(" ", "")
    candidates = result.get("candidates")
    records = candidates if isinstance(candidates, list) and candidates else [result]
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate_id = _normalize_match_text(str(record.get("rera_id") or "")).replace(
            " ", ""
        )
        if expected_id and candidate_id != expected_id:
            continue
        candidate_name = str(record.get("project_name") or "").strip()
        if candidate_name and (
            _contains_entity(candidate_name, [project_name])
            or _contains_entity(project_name, [candidate_name])
        ):
            return True
    return False


async def _discover_rera_reference(
    audit_id: str,
    project_name: str,
    builder: str,
    locality: str,
    city: str,
    property_details: dict[str, Any],
    attempt: int,
) -> tuple[dict[str, str] | None, str | None]:
    address = str(property_details.get("property_address") or "").strip()
    extra_terms = " ".join(
        str(property_details.get(field) or "").strip()
        for field in ("carpet_area", "price")
        if property_details.get(field)
    )
    query = " ".join(
        term
        for term in (
            f'"{project_name}"',
            f'"{builder}"' if builder else "",
            f'"{locality}"' if locality else "",
            f'"{city}"' if city else "",
            f'"{address}"' if address else "",
            extra_terms,
            "Karnataka RERA registration number",
            "project",
        )
        if term
    )
    if attempt == 1:
        query += " official project registration details"
    try:
        search_result = await search_web(audit_id, query, "web")
    except (IntegrationError, httpx.HTTPError, ValueError) as error:
        return None, f"Internet search failed ({type(error).__name__}: {error})."

    sources: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in _results(search_result):
        url = item.get("url") or item.get("link")
        if (
            not isinstance(url, str)
            or not url.startswith(("https://", "http://"))
            or url in seen_urls
        ):
            continue
        seen_urls.add(url)
        sources.append(
            {
                "url": url,
                "title": str(item.get("title") or "")[:300],
                "snippet": str(item.get("snippet") or "")[:700],
            }
        )
        if len(sources) == 5:
            break
    if not sources:
        return None, "Internet search returned no pages to verify."
    try:
        fetched = await fetch_content(
            audit_id,
            [source["url"] for source in sources],
            "Find the Karnataka RERA registration ID and exact project name for this specific property.",
        )
    except (IntegrationError, httpx.HTTPError, ValueError) as error:
        return None, f"Search result fetch failed ({type(error).__name__}: {error})."

    pages_by_url: dict[str, str] = {}
    source_by_url = {source["url"]: source for source in sources}
    for page in _fetch_items(fetched):
        url = page.get("url") or page.get("link")
        text = page.get("text") or page.get("markdown") or page.get("content") or ""
        if isinstance(url, str) and url in source_by_url and isinstance(text, str):
            pages_by_url[url] = text[:5000]
    if not pages_by_url:
        return None, "Fetched search results contained no readable page text."
    if not OPENROUTER_API_KEY:
        return None, "OpenRouter is not configured to verify search results."

    context = {
        "project_name": project_name,
        "builder": builder,
        "locality": locality,
        "city": city,
        "property_address": address,
        "listing_details": {
            key: value
            for key, value in property_details.items()
            if key in {"carpet_area", "price", "possession_date", "rera_id"}
            and value
        },
        "pages": pages_by_url,
    }
    schema = {
        "project_name": "exact project name explicitly present on one source page or null",
        "rera_id": "Karnataka RERA registration ID explicitly present on the same page or null",
        "source_url": "URL of the source page that explicitly shows the project or ID, or null",
    }
    try:
        candidate = await openrouter_chat_json(
            (
                "Find the RERA registration details for the exact property in the supplied "
                "listing context. Use only facts explicitly present in one supplied source "
                "page. Do not guess, combine different projects, or infer a registration ID. "
                "The source must identify the same project and match the builder, locality, "
                "or a distinctive part of the full property address. Return null fields if "
                "the match cannot be proven. Return JSON matching "
                + json.dumps(schema)
            ),
            json.dumps(context, ensure_ascii=False)[:18000],
            timeout_seconds=45,
        )
    except (IntegrationError, httpx.HTTPError, ValueError, KeyError) as error:
        return None, f"Search result verification failed ({type(error).__name__}: {error})."

    source_url = candidate.get("source_url")
    source_text = pages_by_url.get(source_url) if isinstance(source_url, str) else None
    candidate_name = candidate.get("project_name")
    candidate_id = candidate.get("rera_id")
    if (
        not source_text
        or not isinstance(candidate_name, str)
        or not candidate_name.strip()
        or not _contains_entity(source_text, [candidate_name])
    ):
        return None, "Search results did not verify an exact project-name match."

    identity_terms = [builder, locality]
    identity_terms.extend(
        part.strip()
        for part in address.split(",")
        if len(_normalize_match_text(part)) >= 5
            and _normalize_match_text(part) not in {
                _normalize_match_text(city),
                "karnataka",
            }
    )
    if not any(
        _contains_entity(source_text, [term])
        for term in identity_terms
        if term
    ):
        return None, "Search result did not match the builder or property location."

    verified_id = None
    if isinstance(candidate_id, str) and candidate_id.strip():
        normalized_id = _normalize_match_text(candidate_id).replace(" ", "")
        normalized_source = _normalize_match_text(source_text).replace(" ", "")
        if normalized_id in normalized_source:
            verified_id = candidate_id.strip()
    return {
        "project_name": candidate_name.strip(),
        "rera_id": verified_id or "",
    }, None


async def lookup_rera_project(
    audit_id: str,
    project_name: str,
    builder: str,
    locality: str,
    city: str,
    rera_id: str | None,
    property_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    property_details = property_details or {}
    query_project_name = project_name
    query_rera_id = (rera_id or "").strip() or None
    errors: list[str] = []
    for attempt in range(3):
        if query_rera_id:
            search_instruction = (
                f'Use ONLY the Karnataka RERA "Registration ID" search field. Enter '
                f'exactly "{query_rera_id}". Leave every other search field empty; '
                "do not also fill project name, promoter, locality, or city."
            )
        else:
            search_instruction = (
                f'Use ONLY the Karnataka RERA "Project Name" search field. Enter '
                f'exactly "{query_project_name}". Leave every other search field empty; '
                "do not also fill registration ID, promoter, locality, or city."
            )
        goal = (
            "Search the Karnataka RERA portal. " + search_instruction + " Inspect the "
            "results and open the exact matching project's official detail page. Use "
            f"these details only to verify the result after searching: expected project "
            f'"{query_project_name}", builder "{builder}", locality "{locality}", city '
            f'"{city}", property address "{property_details.get("property_address") or ""}". '
            "Never enter those verification details into the portal search form. Return "
            "official registration status, RERA ID, exact project name, promoter, "
            "original and revised completion dates, extension count, complaint count, "
            "and canonical project detail URL. Return dates in YYYY-MM-DD format. If "
            "multiple plausible matches remain, return up to three candidates and do "
            "not choose between them. A portal/network error is not proof of "
            "non-registration."
        )
        try:
            result = await agent_request(
                audit_id,
                goal,
                KARNATAKA_RERA_URL,
                RERA_SCHEMA,
                browser_profile="stealth",
            )
            validation_error = _rera_validation_error(result)
            if not validation_error and result.get("found") is True:
                if _rera_result_matches(result, query_project_name, query_rera_id):
                    return result
                validation_error = (
                    "The portal result did not match the requested project name or registration ID."
                )
            status = str(result.get("status") or "").strip()
            errors.append(
                f"Portal attempt {attempt + 1}: "
                f"{status + ' ' if status else ''}{validation_error or 'no verified project record.'}"
            )
        except (IntegrationError, httpx.HTTPError, ValueError, KeyError) as error:
            errors.append(
                f"Portal attempt {attempt + 1} failed ({type(error).__name__}: {error})."
            )
        if attempt == 2:
            break

        discovered, discovery_error = await _discover_rera_reference(
            audit_id,
            query_project_name,
            builder,
            locality,
            city,
            property_details,
            attempt,
        )
        if discovered:
            query_project_name = discovered["project_name"]
            query_rera_id = discovered["rera_id"] or None
        elif discovery_error:
            errors.append(f"Fallback attempt {attempt + 1}: {discovery_error}")

    return {
        "found": False,
        "rera_id": query_rera_id or rera_id,
        "source_error": "Karnataka RERA could not be verified after three portal "
        "attempts. " + " ".join(errors),
    }


async def classify_sources(
    news: list[dict[str, Any]],
    residents: list[dict[str, Any]],
    locality: list[dict[str, Any]] | None = None,
    subject: dict[str, str | None] | None = None,
) -> None:
    groups = (
        ("news", news),
        ("resident_forum", residents),
        ("locality", locality or []),
    )
    sources = [
        {
            "url": item["url"],
            "title": item.get("title"),
            "source_type": item.get("source_type", source_type),
            "scope": item.get("source_scope", "project"),
            "text": item["_source_text"][:3500],
        }
        for source_type, group in groups
        for item in group
        if item.get("url") and item.get("_source_text")
    ]
    if not sources:
        return
    audit_subject = subject or {}
    classifications: dict[tuple[str, str], dict[str, Any]] = {}
    for start in range(0, len(sources), 4):
        answer = await openrouter_chat_json(
            (
                "You are preparing an evidence-backed real-estate research report. "
                "Return one result for every supplied source, preserving its exact URL "
                "and source_type. Determine relevance from the page title, fetched text, "
                "and audit subject; do not infer a match from city name alone. A project "
                "source must explicitly mention the project or builder. A locality "
                "source must explicitly mention the named locality/address area. Mark "
                "negative only for explicit adverse coverage about the project or "
                "builder, never for neighborhood-only news. For resident sources choose "
                "an explicitly supported issue_category from waterlogging, flooding, "
                "drainage, power, water_supply, structural, traffic, safety, air_quality, "
                "noise, other, or null. Return a factual summary of at most 240 "
                "characters grounded only in an exact supporting excerpt of at most 320 "
                "characters. Never fabricate or paraphrase the excerpt. JSON shape: "
                "{items:[{url,source_type,relevant,negative,issue_category,topic,"
                "summary,excerpt}]}. Use empty strings for unsupported summary/excerpt."
            ),
            json.dumps(
                {"audit_subject": audit_subject, "sources": sources[start : start + 4]},
                ensure_ascii=False,
            ),
            timeout_seconds=45,
        )
        batch_items = answer.get("items")
        if not isinstance(batch_items, list):
            raise ValueError("AI source classification did not return an items array.")
        for item in batch_items:
            if not isinstance(item, dict):
                continue
            source_url = item.get("url")
            source_type = item.get("source_type")
            if isinstance(source_url, str) and isinstance(source_type, str):
                classifications[(source_url, source_type)] = item

    item_by_url = {
        (item["url"], item.get("source_type", source_type)): item
        for source_type, group in groups
        for item in group
        if item.get("url")
    }
    allowed_categories = {
        "waterlogging",
        "flooding",
        "drainage",
        "power",
        "water_supply",
        "structural",
        "traffic",
        "safety",
        "air_quality",
        "noise",
        "other",
    }
    audit_subject = subject or {}
    locality_entities = _locality_entities(audit_subject)
    project_entities = [
        value.strip()
        for value in (audit_subject.get("project_name"), audit_subject.get("builder"))
        if isinstance(value, str) and value.strip()
    ]
    for (source_url, source_type), item in item_by_url.items():
        classification = classifications.get((source_url, source_type))
        if not classification:
            item["classification"] = {
                "relevant": False,
                "negative": False,
                "issue_category": None,
                "topic": None,
                "summary": None,
                "excerpt": "",
                "verified": False,
            }
            continue
        excerpt = str(classification.get("excerpt") or "").strip()
        source_text = re.sub(r"\s+", " ", item.get("_source_text", "")).strip()
        normalized_source = source_text.casefold()
        normalized_excerpt = re.sub(r"\s+", " ", excerpt).strip().casefold()
        quote_verified = bool(
            normalized_excerpt and normalized_excerpt in normalized_source
        )
        source_scope = item.get("source_scope", "project")
        entities = locality_entities if source_scope == "locality" else project_entities
        identity_text = f"{item.get('title') or ''} {source_text}"
        identity_verified = _contains_entity(identity_text, entities)
        verified = quote_verified and identity_verified
        relevant = classification.get("relevant") is True and identity_verified
        category = classification.get("issue_category")
        summary = classification.get("summary")
        item["classification"] = {
            "relevant": relevant,
            "negative": (
                source_type == "news"
                and source_scope == "project"
                and relevant
                and classification.get("negative") is True
            ),
            "issue_category": (
                category
                if relevant
                and isinstance(category, str)
                and category in allowed_categories
                else None
            ),
            "topic": (
                classification["topic"].strip()[:100]
                if isinstance(classification.get("topic"), str)
                else None
            ),
            "summary": (
                summary.strip()[:240]
                if verified and isinstance(summary, str) and summary.strip()
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
            "property_address": {"type": "string", "nullable": True},
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
    extracted: dict[str, Any] = {}
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
                extracted = await llm_extract_claims(audit_id, text)
        except (
            IntegrationError,
            httpx.HTTPError,
            ValueError,
            KeyError,
            IndexError,
        ):
            extracted = {}

    goal = (
        "Read this property listing and extract only claims explicitly shown on the page. "
        "Return the full property address exactly as listed in property_address. Also "
        "extract locality (neighborhood, suburb or area) separately when explicitly "
        "named on the listing or clearly present in its address; do not substitute the "
        "city or invent a locality. Return the requested JSON schema. Use null for every "
        "missing field; never infer or guess."
    )
    required_fields = ("project_name", "builder", "city")
    if all(extracted.get(field) for field in required_fields):
        return extracted
    try:
        browser_claims = await agent_request(
            audit_id, goal, listing_url, schema, browser_profile="stealth"
        )
    except (IntegrationError, httpx.HTTPError, ValueError, KeyError) as error:
        return {
            **extracted,
            "extraction_error": (
                f"Listing page automation failed ({type(error).__name__}): {error}"
            ),
        }
    return {
        **{
            key: value
            for key, value in browser_claims.items()
            if value is not None and value != ""
        },
        **{
            key: value
            for key, value in extracted.items()
            if value is not None and value != ""
        },
    }


async def llm_extract_claims(audit_id: str, page_text: str) -> dict[str, Any]:
    del audit_id
    schema = {
        "project_name": "string or null",
        "builder": "string or null",
        "property_address": "string or null",
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
            "Extract only facts explicitly present in the supplied listing text. Return "
            "the complete property address in property_address. Identify locality as "
            "the neighborhood, suburb or named area in the provided address/listing; "
            "do not use the city as locality and do not guess. Never guess; use null for "
            "missing values. Return JSON matching "
            + json.dumps(schema)
        ),
        page_text[:18000],
        timeout_seconds=45,
    )
    expected_fields = {
        "project_name",
        "builder",
        "property_address",
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
    audit_id: str,
    project_name: str,
    builder: str,
    locality_name: str | None,
    city: str,
    property_address: str | None = None,
) -> dict[str, Any]:
    locality_query_name = _locality_search_name(
        locality_name, property_address, city
    )
    searches = (
        (
            "news",
            "project",
            f'"{builder}" "{project_name}" delay OR complaint OR RERA OR dispute',
            "news",
            None,
        ),
        (
            "news",
            "locality",
            f'"{locality_query_name}" "{city}" (waterlogging OR flooding OR drainage OR traffic OR civic OR safety)',
            "news",
            None,
        ),
        (
            "resident_forum",
            "project",
            f'"{project_name}" (waterlogging OR seepage OR "power cuts" OR "water supply" OR structural OR traffic)',
            "web",
            "reddit.com",
        ),
        (
            "resident_forum",
            "locality",
            f'"{locality_query_name}" "{city}" (waterlogging OR flooding OR drainage OR "power cuts" OR "water supply" OR traffic)',
            "web",
            "reddit.com",
        ),
        (
            "resident_forum",
            "project",
            f'"{project_name}" (review OR resident OR complaint OR experience OR forum) (waterlogging OR seepage OR "power cuts" OR "water supply" OR structural OR traffic)',
            "web",
            None,
        ),
        (
            "resident_forum",
            "locality",
            f'"{locality_query_name}" "{city}" (resident OR review OR forum OR complaint) (waterlogging OR flooding OR drainage OR "power cuts" OR "water supply" OR traffic)',
            "web",
            None,
        ),
        (
            "public_web",
            "locality",
            f'"{locality_query_name}" "{city}" (civic OR flooding OR drainage OR traffic OR "water supply" OR power OR safety)',
            "web",
            None,
        ),
    )
    results = await asyncio.gather(
        *(
            search_web(audit_id, query, domain_type, domains)
            for _, _, query, domain_type, domains in searches
        ),
        return_exceptions=True,
    )
    groups: dict[str, list[dict[str, Any]]] = {
        "news": [],
        "residents": [],
        "locality": [],
    }
    search_failures = 0
    search_status: dict[str, dict[str, Any]] = {}
    for (source_type, scope, query, _, _), result in zip(searches, results):
        if source_type == "news":
            status_key = "news" if scope == "project" else "locality_news"
        elif source_type == "resident_forum":
            status_key = "residents" if scope == "project" else "locality_residents"
        else:
            status_key = "locality_web"
        if isinstance(result, Exception):
            search_failures += 1
            previous = search_status.get(status_key)
            search_status[status_key] = {
                "status": (
                    "completed"
                    if previous and previous["status"] == "completed"
                    else "failed"
                ),
                "count": previous["count"] if previous else 0,
                "message": " ".join(
                    part
                    for part in (
                        previous["message"] if previous else "",
                        f"Search failed: {result}",
                    )
                    if part
                ),
            }
            continue
        items = _results(result)[:3]
        for item in items:
            item["url"] = item.get("url") or item.get("link") or item.get("source_url")
            item["source_type"] = source_type
            item["source_scope"] = scope
            item["trace_id"] = result.get("trueestate_trace_id")
            item["search_query"] = query
            if not item.get("date"):
                item["date"] = item.get("published_at") or item.get("published_date")
            if scope == "project" and source_type == "news":
                groups["news"].append(item)
            elif scope == "project":
                groups["residents"].append(item)
            else:
                groups["locality"].append(item)
        previous = search_status.get(status_key)
        search_status[status_key] = {
            "status": "completed",
            "count": (previous["count"] if previous else 0) + len(items),
            "message": " ".join(
                part
                for part in (
                    previous["message"] if previous else "",
                    (
                        f"{len(items)} public result(s) found."
                        if items
                        else "Search completed; no matching public results were returned."
                    ),
                )
                if part
            ),
        }

    for group_name, items in groups.items():
        seen: set[str] = set()
        unique_items = []
        for item in items:
            url = item.get("url")
            if (
                not isinstance(url, str)
                or not url.startswith(("http://", "https://"))
                or url in seen
            ):
                continue
            seen.add(url)
            unique_items.append(item)
        groups[group_name] = unique_items[:
            3 if group_name == "news" else 6 if group_name == "residents" else 9
        ]

    seen_urls: set[str] = set()
    for group_name in ("news", "residents", "locality"):
        unique_items = []
        for item in groups[group_name]:
            if item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            unique_items.append(item)
        groups[group_name] = unique_items

    all_items = groups["news"] + groups["residents"] + groups["locality"]
    unique_urls = list(
        dict.fromkeys(item["url"] for item in all_items if item.get("url"))
    )[:18]
    fetch_failed = False
    fetch_errors: list[str] = []
    if unique_urls:
        texts: dict[str, tuple[str, str | None]] = {}
        for start in range(0, len(unique_urls), 10):
            url_batch = unique_urls[start : start + 10]
            try:
                fetched = await fetch_content(
                    audit_id,
                    url_batch,
                    "Retrieve source text for an evidence-linked property due diligence report.",
                )
            except (IntegrationError, httpx.HTTPError) as error:
                fetch_failed = True
                fetch_errors.append(
                    f"Fetch batch {start // 10 + 1} failed: {type(error).__name__}: {error}"
                )
                continue
            fetch_trace_id = fetched.get("trueestate_trace_id")
            for item in _fetch_items(fetched):
                text = item.get("text") or item.get("content") or item.get("markdown")
                if item.get("url") and isinstance(text, str) and text.strip():
                    texts[item["url"]] = (text, fetch_trace_id)
        for item in all_items:
            fetched_item = texts.get(item.get("url"))
            if fetched_item:
                text, fetch_trace_id = fetched_item
                item["_source_text"] = text
                item["excerpt"] = re.sub(r"\s+", " ", text).strip()[:420]
                item["trace_id"] = fetch_trace_id or item.get("trace_id")
                item["tinyfish_tool"] = "fetch_content"
            else:
                item["excerpt"] = item.get("snippet", "")
                item["fetch_status"] = "failed"
                fetch_failed = True
                if not any(item.get("url") in error for error in fetch_errors):
                    fetch_errors.append(
                        f"No readable fetched text for {item.get('url') or 'a search result'}."
                    )
    else:
        for item in all_items:
            item["excerpt"] = item.get("snippet", "")
    classification_error = None
    classification_status = "skipped"
    unverified_count = 0
    if OPENROUTER_API_KEY:
        has_source_text = any(item.get("_source_text") for item in all_items)
        if has_source_text:
            try:
                await classify_sources(
                    groups["news"],
                    groups["residents"],
                    groups["locality"],
                    {
                        "project_name": project_name,
                        "builder": builder,
                        "locality": locality_name,
                        "property_address": property_address,
                        "city": city,
                    },
                )
                classification_status = "completed"
                for group_name in ("news", "residents", "locality"):
                    kept = []
                    for item in groups[group_name]:
                        classification = item.get("classification", {})
                        if (
                            classification.get("verified") is True
                            and classification.get("relevant") is True
                        ):
                            kept.append(item)
                        else:
                            unverified_count += 1
                    groups[group_name] = kept
                all_items = groups["news"] + groups["residents"] + groups["locality"]
            except (
                IntegrationError,
                httpx.HTTPError,
                ValueError,
                KeyError,
                IndexError,
            ) as error:
                classification_error = f"{type(error).__name__}: {error}"
                classification_status = "failed"
        else:
            classification_error = (
                "No readable article or forum text was returned for classification."
            )
    if classification_status != "completed":
        unverified_count = len(all_items)
        groups = {"news": [], "residents": [], "locality": []}
        all_items = []
    retrieved_at = now_iso()
    for item in all_items:
        item["retrieved_at"] = retrieved_at
        item["tinyfish_tool"] = item.get("tinyfish_tool", "search")
        item["source_name"] = item.get("publisher") or item.get("site_name")
        if classification_status != "completed" and not item.get("classification"):
            item["classification"] = {
                "relevant": False,
                "negative": False,
                "issue_category": None,
                "topic": None,
                "summary": None,
                "excerpt": "",
                "verified": False,
            }
        item.pop("_source_text", None)
    failed = search_failures + int(fetch_failed) + int(classification_status == "failed")
    statuses = {
        "news": search_status.get(
            "news",
            {"status": "failed", "count": 0, "message": "Project news search failed."},
        ),
        "residents": search_status.get(
            "residents",
            {"status": "failed", "count": 0, "message": "Resident search failed."},
        ),
        "locality": {
            "status": (
                "completed"
                if search_status.get("locality_news", {}).get("status") == "completed"
                or search_status.get("locality_residents", {}).get("status") == "completed"
                or search_status.get("locality_web", {}).get("status") == "completed"
                else "failed"
            ),
            "count": len(groups["locality"]),
            "message": (
                "Local news: "
                + search_status.get("locality_news", {}).get(
                    "message", "Search was not completed."
                )
                + " Community forums: "
                + search_status.get("locality_residents", {}).get(
                    "message", "Search was not completed."
                )
                + " General public web: "
                + search_status.get("locality_web", {}).get(
                    "message", "Search was not completed."
                )
            ),
        },
        "classification": {
            "status": classification_status,
            "message": " ".join(
                part
                for part in (
                    " ".join(fetch_errors) if fetch_errors else "",
                    classification_error
                    or (
                        f"Kept {len(all_items)} source(s) with verified quotes and matching "
                        f"project/locality identity; excluded {unverified_count} unrelated "
                        "or unverified result(s)."
                        if classification_status == "completed"
                        else (
                            "No fetched source text was available for classification."
                            if OPENROUTER_API_KEY
                            else "Set OPENROUTER_API_KEY to classify sources; unverified snippets do not affect findings."
                        )
                    ),
                )
                if part
            ),
        },
    }
    if classification_status == "completed":
        statuses["news"]["count"] = len(groups["news"])
        statuses["news"]["message"] += (
            f" Showing {len(groups['news'])} verified relevant source(s)."
        )
        statuses["residents"]["count"] = len(groups["residents"])
        statuses["residents"]["message"] += (
            f" Showing {len(groups['residents'])} verified relevant source(s)."
        )
        statuses["locality"]["message"] += (
            f" Showing {len(groups['locality'])} verified relevant source(s)."
        )
    else:
        for key in ("news", "residents", "locality"):
            statuses[key]["count"] = 0
            statuses[key]["message"] += (
                f" {unverified_count} unverified result(s) were withheld from the report."
            )
    return {
        "news": groups["news"],
        "residents": groups["residents"],
        "locality": groups["locality"],
        "failed_agents": failed,
        "research_status": statuses,
    }


async def collect_builder_projects(
    audit_id: str,
    builder: str,
    city: str,
    primary_rera_id: str | None,
    primary_project_name: str | None = None,
) -> dict[str, Any]:
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
            f"Search Karnataka RERA by promoter name {builder} for up to five "
            f"registered projects, prioritizing projects in or near {city}. Open the "
            "portal results and return only distinct projects whose promoter matches "
            "the supplied builder. Exclude the primary project and return its exact "
            "name, promoter, RERA ID, and official project-detail URL. If the portal "
            "search returns no results, return an empty projects array. Do not invent "
            "IDs or URLs."
        ),
        KARNATAKA_RERA_URL,
        schema,
        browser_profile="stealth",
    )
    projects = discovery.get("projects")
    if not isinstance(projects, list):
        return {
            "projects": [],
            "message": "Builder project search did not return a readable projects list.",
        }
    primary_id = re.sub(r"[^a-z0-9]", "", (primary_rera_id or "").lower())
    primary_name = re.sub(
        r"[^a-z0-9]", "", (primary_project_name or "").lower()
    )
    unique: dict[str, dict[str, Any]] = {}
    rejected = 0
    for project in projects:
        if not isinstance(project, dict):
            rejected += 1
            continue
        url = project.get("project_url")
        project_name = project.get("project_name")
        rera_id = project.get("rera_id")
        if (
            not is_official_rera_url(url)
            or not isinstance(project_name, str)
            or not project_name.strip()
            or not isinstance(rera_id, str)
            or not rera_id.strip()
        ):
            rejected += 1
            continue
        normalized_id = re.sub(r"[^a-z0-9]", "", rera_id.lower())
        normalized_name = re.sub(
            r"[^a-z0-9]", "", project_name.lower()
        )
        if (
            (primary_id and normalized_id == primary_id)
            or (primary_name and normalized_name == primary_name)
        ):
            continue
        unique[normalized_id or url] = project

    async def inspect(project: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
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
                browser_profile="stealth",
            )
        except (IntegrationError, httpx.HTTPError, ValueError, KeyError) as error:
            return None, f"{project['project_name']}: {type(error).__name__}: {error}"
        detail["project_url"] = detail.get("project_url") or project["project_url"]
        validation_error = _rera_validation_error(detail)
        if validation_error:
            return None, f"{project['project_name']}: {validation_error}"
        return ({**project, **detail, "retrieved_at": now_iso()}, None)

    inspections = await asyncio.gather(
        *(inspect(project) for project in list(unique.values())[:5])
    )
    verified_projects = [
        project for project, _ in inspections if project is not None
    ]
    detail_failures = [error for _, error in inspections if error]

    async def find_public_sources(
        project: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        project_name = str(project.get("project_name") or "").strip()
        promoter = str(project.get("promoter") or builder).strip()
        try:
            result = await search_web(
                audit_id,
                f'"{project_name}" "{promoter}" project review OR news OR resident',
                "web",
            )
        except (IntegrationError, httpx.HTTPError, ValueError) as error:
            return project, [f"{project_name}: public search failed ({type(error).__name__})."]
        public_sources = []
        seen_urls: set[str] = set()
        for item in _results(result):
            url = item.get("url") or item.get("link")
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if (
                not isinstance(url, str)
                or not url.startswith(("https://", "http://"))
                or url in seen_urls
                or not _contains_entity(f"{title} {snippet}", [project_name])
            ):
                continue
            seen_urls.add(url)
            public_sources.append(
                {
                    "title": title or project_name,
                    "url": url,
                    "source_name": item.get("publisher") or item.get("site_name"),
                    "snippet": snippet[:320],
                }
            )
            if len(public_sources) == 3:
                break
        project["public_sources"] = public_sources
        return project, []

    public_results = await asyncio.gather(
        *(find_public_sources(project) for project in verified_projects)
    )
    verified_projects = [project for project, _ in public_results]
    public_search_failures = [
        error for _, errors in public_results for error in errors
    ]
    if verified_projects:
        message = (
            f"Verified {len(verified_projects)} other RERA project(s); "
            f"{sum(len(project['public_sources']) for project in verified_projects)} "
            "matching public reference(s) found."
        )
    elif unique:
        message = "Builder projects were discovered, but their official detail pages could not be verified."
    else:
        message = "Builder search completed; no additional verifiable RERA projects were returned."
    failures = detail_failures + public_search_failures
    if rejected:
        failures.append(f"Skipped {rejected} discovery result(s) without a valid name, ID, and official URL.")
    if failures:
        message += " " + " ".join(failures)
    return {"projects": verified_projects, "message": message}


async def run_audit(audit_id: str) -> None:
    audit = database.load_audit(audit_id)
    if not audit:
        return
    try:
        await emit(audit_id, "extract", "started", "Preparing listing claims.")
        claims = audit["claims"]
        extraction_error = None
        listing_url = audit.get("listing_url")
        if listing_url:
            if any(
                not claims.get(field)
                for field in (
                    "project_name",
                    "builder",
                    "city",
                    "locality",
                    "property_address",
                )
            ):
                try:
                    extracted = await extract_listing(audit_id, listing_url)
                    extraction_error = extracted.pop("extraction_error", None)
                    claims = {
                        **claims,
                        **{
                            key: value
                            for key, value in extracted.items()
                            if value is not None
                        },
                    }
                except (IntegrationError, httpx.HTTPError, ValueError, KeyError) as error:
                    extraction_error = (
                        f"Listing extraction failed ({type(error).__name__}): {error}"
                    )
                    await emit(
                        audit_id,
                        "extract",
                        "failed",
                        extraction_error,
                    )
            audit = _update_audit(audit_id, claims=claims)
        if claims.get("project_name") and claims.get("builder"):
            await emit(audit_id, "extract", "completed", "Listing claims are ready.")

        project = claims.get("project_name") or audit.get("project_name")
        builder = claims.get("builder") or audit.get("builder")
        locality_name = claims.get("locality") or audit.get("locality_name")
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
            skipped_research = {
                key: {
                    "status": "skipped",
                    "count": 0,
                    "message": f"Not searched: live RERA audits are not configured for {state}.",
                }
                for key in ("news", "residents", "locality", "builder")
            }
            skipped_research["classification"] = {
                "status": "skipped",
                "message": "Source classification was skipped because research did not run.",
            }
            _update_audit(
                audit_id,
                status="unsupported",
                project_name=project,
                builder=builder,
                locality_name=locality_name,
                city=city,
                state=state,
                score=calculate_score(None, [], failed_agents=1),
                research_status=skipped_research,
            )
            return
        if not all((project, builder, city)):
            missing = [
                field
                for field, value in (
                    ("project name", project),
                    ("builder", builder),
                    ("city", city),
                )
                if not value
            ]
            message = (
                f"Listing details are incomplete; could not confirm {', '.join(missing)}."
            )
            if extraction_error:
                message += f" {extraction_error}"
            message += " Add the missing details below to continue the audit."
            skipped_research = {
                key: {
                    "status": "skipped",
                    "count": 0,
                    "message": message,
                }
                for key in ("news", "residents", "locality", "builder")
            }
            skipped_research["classification"] = {
                "status": "skipped",
                "message": message,
            }
            _update_audit(
                audit_id,
                status="needs_input",
                claims=claims,
                project_name=project,
                builder=builder,
                locality_name=locality_name,
                city=city,
                state=state,
                research_status=skipped_research,
            )
            await emit(
                audit_id,
                "extract",
                "failed",
                message,
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
        await emit(audit_id, "news", "started", "Searching recent project and builder news.")
        await emit(audit_id, "residents", "started", "Searching public resident forums.")
        await emit(
            audit_id,
            "locality",
            "started",
            f"Researching public locality reports for {locality_name or city}.",
        )
        rera_task = lookup_rera_project(
            audit_id,
            str(project),
            str(builder),
            str(locality_name or city),
            str(city),
            str(claims.get("rera_id") or ""),
            claims,
        )
        evidence_task = gather_evidence(
            audit_id,
            str(project),
            str(builder),
            str(locality_name) if locality_name else None,
            str(city),
            (
                str(claims.get("property_address"))
                if claims.get("property_address")
                else None
            ),
        )
        builder_task = collect_builder_projects(
            audit_id,
            str(builder),
            str(city),
            claims.get("rera_id"),
            str(project) if project else None,
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
            candidates = record.get("candidates")
            validation_error = (
                record.get("source_error") or _rera_validation_error(record)
            )
            if (
                isinstance(candidates, list)
                and len(candidates) > 1
                and validation_error is None
            ):
                record["ambiguous"] = True
                record["found"] = False
                record["project_url"] = None
                await emit(
                    audit_id,
                    "rera",
                    "completed",
                    "Multiple plausible RERA projects found; select the correct record.",
                )
            elif validation_error:
                record["found"] = False
                record["source_error"] = validation_error
                failures += 1
                await emit(
                    audit_id,
                    "rera",
                    "failed",
                    f"RERA result was not verified: {validation_error}",
                )
            else:
                await emit(audit_id, "rera", "completed", "RERA lookup finished.")

        if isinstance(evidence_result, Exception):
            failures += 1
            news, residents, locality_results = [], [], []
            research_status = {
                key: {"status": "failed", "count": 0, "message": str(evidence_result)}
                for key in ("news", "residents", "locality")
            }
            research_status["classification"] = {
                "status": "skipped",
                "message": "Source research did not complete.",
            }
        else:
            news = evidence_result["news"]
            residents = evidence_result["residents"]
            locality_results = evidence_result["locality"]
            research_status = evidence_result["research_status"]
            failures += evidence_result["failed_agents"]
        if isinstance(builder_result, Exception):
            builder_projects = []
            failures += 1
            research_status["builder"] = {
                "status": "failed",
                "count": 0,
                "message": f"Builder portfolio lookup failed: {builder_result}",
            }
            await emit(
                audit_id,
                "builder",
                "failed",
                f"Builder portfolio lookup failed: {builder_result}",
            )
        else:
            builder_data = builder_result
            builder_projects = (
                builder_data.get("projects", [])
                if isinstance(builder_data, dict)
                else []
            )
            builder_message = (
                str(builder_data.get("message") or "")
                if isinstance(builder_data, dict)
                else "Builder lookup returned an invalid response."
            )
            if not isinstance(builder_projects, list):
                builder_projects = []
                builder_message = "Builder lookup returned an invalid projects list."
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
            research_status["builder"] = {
                "status": "completed",
                "count": len(builder_projects),
                "message": builder_message,
            }
            await emit(
                audit_id,
                "builder",
                "completed",
                builder_message,
            )
        classification_status = research_status["classification"]["status"]
        if classification_status == "skipped":
            await emit(
                audit_id,
                "classification",
                "skipped",
                research_status["classification"]["message"],
            )
        elif classification_status == "failed":
            await emit(
                audit_id,
                "classification",
                "failed",
                f"Source classification failed: {research_status['classification']['message']}. Results remain unverified.",
            )
        else:
            await emit(
                audit_id,
                "classification",
                "completed",
                research_status["classification"]["message"],
            )
        await emit(
            audit_id,
            "news",
            research_status["news"]["status"],
            research_status["news"]["message"],
        )
        await emit(
            audit_id,
            "residents",
            research_status["residents"]["status"],
            research_status["residents"]["message"],
        )
        await emit(
            audit_id,
            "locality",
            research_status["locality"]["status"],
            research_status["locality"]["message"],
        )

        retrieved_at = now_iso()
        findings = compare_claims(claims, record, retrieved_at)
        findings.extend(
            evidence_findings(news, residents, retrieved_at, locality_results)
        )
        locality_news_count = sum(
            item.get("source_type") == "news" for item in locality_results
        )
        locality_resident_count = sum(
            item.get("source_type") == "resident_forum"
            for item in locality_results
        )
        score = calculate_score(
            record,
            findings,
            builder_projects=builder_projects,
            news_count=len(news) + locality_news_count,
            resident_count=len(residents) + locality_resident_count,
            failed_agents=failures,
        )
        status = (
            "completed"
            if record and not record.get("source_error") and not record.get("ambiguous")
            else "partial"
        )
        await emit(audit_id, "audit", "completed", "Audit report is ready.")
        _update_audit(
            audit_id,
            status=status,
            claims=claims,
            locality_name=locality_name,
            rera_record=record,
            findings=findings,
            news=news,
            residents=residents,
            locality=locality_results,
            builder_projects=builder_projects,
            research_status=research_status,
            score=score,
        )
    except Exception as error:
        audit = database.load_audit(audit_id) or {}
        research_status = audit.get("research_status", {})
        for section in ("news", "residents", "locality", "builder"):
            if research_status.get(section, {}).get("status") == "pending":
                research_status[section] = {
                    "status": "failed",
                    "count": 0,
                    "message": str(error),
                }
        _update_audit(
            audit_id,
            status="failed",
            error=str(error),
            research_status=research_status,
        )
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
        "locality": payload.locality,
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
        "locality_name": payload.locality,
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
        "locality": [],
        "research_status": {
            "news": {"status": "pending", "count": 0, "message": "Search not started."},
            "residents": {"status": "pending", "count": 0, "message": "Search not started."},
            "locality": {"status": "pending", "count": 0, "message": "Search not started."},
            "builder": {
                "status": "pending",
                "count": 0,
                "message": "Builder project search not started.",
            },
            "classification": {
                "status": "pending",
                "message": "Source classification not started.",
            },
        },
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


@app.post("/api/audits/{audit_id}/retry", status_code=202)
async def retry_audit(
    audit_id: str, payload: AuditRetry | None = None
) -> dict[str, Any]:
    audit = database.load_audit(audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail="Audit not found.")
    if audit.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="This audit is already running.")
    if audit.get("status") not in {"partial", "failed", "needs_input"}:
        raise HTTPException(
            status_code=409,
            detail="Only partial, failed, or input-needed audits can be retried.",
        )
    claims = {**(audit.get("claims") or {})}
    if payload:
        claims.update(payload.model_dump(exclude_unset=True, exclude_none=True))
    if not all(
        claims.get(field) or audit.get(field)
        for field in ("project_name", "builder", "city")
    ):
        missing = [
            field
            for field in ("project_name", "builder", "city")
            if not (claims.get(field) or audit.get(field))
        ]
        raise HTTPException(
            status_code=422,
            detail=f"Provide the missing required details: {', '.join(missing)}.",
        )
    pending = {
        "status": "pending",
        "count": 0,
        "message": "Retry not started.",
    }
    _update_audit(
        audit_id,
        status="queued",
        claims=claims,
        project_name=claims.get("project_name") or audit.get("project_name"),
        builder=claims.get("builder") or audit.get("builder"),
        locality_name=claims.get("locality") or audit.get("locality_name"),
        city=claims.get("city") or audit.get("city"),
        state=claims.get("state") or audit.get("state") or "Karnataka",
        error=None,
        rera_record=None,
        findings=[],
        news=[],
        residents=[],
        locality=[],
        builder_projects=[],
        research_status={
            section: {**pending}
            for section in ("news", "residents", "locality", "builder", "classification")
        },
        score=calculate_score(None, [], failed_agents=1),
        progress=[],
    )
    await emit(audit_id, "audit", "started", "Retrying live property research.")
    asyncio.create_task(run_audit(audit_id))
    return {"id": audit_id, "status": "queued"}


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
        evidence_findings(
            audit.get("news", []),
            audit.get("residents", []),
            now_iso(),
            audit.get("locality", []),
        )
    )
    locality_results = audit.get("locality", [])
    score = calculate_score(
        record,
        findings,
        builder_projects=audit.get("builder_projects", []),
        news_count=len(audit["news"])
        + sum(item.get("source_type") == "news" for item in locality_results),
        resident_count=len(audit["residents"])
        + sum(
            item.get("source_type") == "resident_forum"
            for item in locality_results
        ),
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
