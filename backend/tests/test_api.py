import json as json_lib
import time

import httpx

from fastapi.testclient import TestClient

from app import database, main


def test_health_and_karnataka_only_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    monkeypatch.setattr(main, "TINYFISH_API_KEY", "")
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")

    with TestClient(main.app) as client:
        health = client.get("/api/health")
        unsupported = client.post(
            "/api/audits",
            json={
                "project_name": "Example Homes",
                "builder": "Example Group",
                "city": "Pune",
                "state": "Maharashtra",
            },
        )

    assert health.json()["status"] == "ok"
    assert health.json()["tinyfish_configured"] is False
    assert health.json()["llm_configured"] is False
    assert health.json()["llm_provider"] == "OpenRouter"
    assert health.json()["llm_model"] == "openrouter/free"
    assert unsupported.status_code == 422
    assert "Karnataka only" in unsupported.json()["detail"]


def test_manual_audit_persists_unavailable_live_runs_without_fabricating_data(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    monkeypatch.setattr(main, "TINYFISH_API_KEY", "")

    with TestClient(main.app) as client:
        created = client.post(
            "/api/audits",
            json={
                "project_name": "Example Homes",
                "builder": "Example Group",
                "city": "Bengaluru",
            },
        )
        assert created.status_code == 202
        audit_id = created.json()["id"]

        for _ in range(50):
            audit = client.get(f"/api/audits/{audit_id}").json()
            if audit["status"] == "partial":
                break
            time.sleep(0.02)
        runs = client.get(f"/api/audits/{audit_id}/runs").json()
        events = client.get(f"/api/audits/{audit_id}/events")

    assert audit["status"] == "partial"
    assert audit["score"]["score"] is None
    assert audit["score"]["confidence"] == "Low"
    assert runs
    assert all(run["status"] == "FAILED" for run in runs)
    assert all("TINYFISH_API_KEY is not configured" in run["raw_output_json"]["error"] for run in runs)
    assert events.status_code == 200
    assert '"stage": "audit"' in events.text


def test_partial_audit_can_retry_and_resets_stale_results(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    started_audits = []

    async def fake_run_audit(audit_id):
        started_audits.append(audit_id)

    monkeypatch.setattr(main, "run_audit", fake_run_audit)
    audit_id = "retryable-audit"
    database.initialize()
    database.save_audit({
        "id": audit_id,
        "created_at": "2026-11-01T00:00:00Z",
        "status": "partial",
        "claims": {
            "project_name": "Example Homes",
            "builder": "Example Group",
            "city": "Bengaluru",
        },
        "progress": [],
        "rera_record": {"found": False},
        "findings": [{"title": "stale"}],
        "news": [{"title": "stale"}],
        "residents": [{"title": "stale"}],
        "locality": [{"title": "stale"}],
        "builder_projects": [{"project_name": "stale"}],
    })

    with TestClient(main.app) as client:
        response = client.post(f"/api/audits/{audit_id}/retry")
        audit = client.get(f"/api/audits/{audit_id}").json()

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert audit["status"] == "queued"
    assert audit["rera_record"] is None
    assert audit["findings"] == []
    assert audit["news"] == []
    assert audit["residents"] == []
    assert audit["locality"] == []
    assert audit["builder_projects"] == []
    assert audit["research_status"]["builder"]["status"] == "pending"
    assert started_audits == [audit_id]


def test_agent_console_runs_are_isolated_by_audit_id(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    database.initialize()
    for audit_id in ("old-audit", "new-audit"):
        database.save_audit({
            "id": audit_id,
            "created_at": "2026-11-01T00:00:00Z",
        })
    with database.connect() as connection:
        for run_id, audit_id, tool in (
            ("old-run-1", "old-audit", "search"),
            ("old-run-2", "old-audit", "fetch_content"),
            ("new-run-1", "new-audit", "run_web_automation"),
            ("new-run-2", "new-audit", "search"),
        ):
            connection.execute(
                "INSERT INTO runs (id, audit_id, tool, input_summary, status, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    audit_id,
                    tool,
                    f"{audit_id} {tool}",
                    "COMPLETED",
                    "2026-11-01T00:00:00Z",
                ),
            )

    with TestClient(main.app) as client:
        old_runs = client.get("/api/audits/old-audit/runs")
        new_runs = client.get("/api/audits/new-audit/runs")

    assert old_runs.status_code == 200
    assert new_runs.status_code == 200
    assert [run["id"] for run in old_runs.json()] == ["old-run-1", "old-run-2"]
    assert [run["id"] for run in new_runs.json()] == ["new-run-1", "new-run-2"]
    assert all(run["audit_id"] == "new-audit" for run in new_runs.json())


def test_needs_input_audit_can_resume_with_manual_listing_details(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    started_audits = []

    async def fake_run_audit(audit_id):
        started_audits.append(audit_id)

    monkeypatch.setattr(main, "run_audit", fake_run_audit)
    audit_id = "needs-input-audit"
    database.initialize()
    database.save_audit({
        "id": audit_id,
        "created_at": "2026-11-01T00:00:00Z",
        "status": "needs_input",
        "claims": {
            "project_name": "Example Homes",
            "builder": None,
            "city": "Bengaluru",
            "locality": "Whitefield",
        },
        "progress": [],
        "findings": [],
        "news": [],
        "residents": [],
        "locality": [],
        "builder_projects": [],
    })

    with TestClient(main.app) as client:
        response = client.post(
            f"/api/audits/{audit_id}/retry",
            json={"builder": "Example Group"},
        )
        audit = client.get(f"/api/audits/{audit_id}").json()

    assert response.status_code == 202
    assert audit["status"] == "queued"
    assert audit["claims"]["builder"] == "Example Group"
    assert audit["builder"] == "Example Group"
    assert audit["locality_name"] == "Whitefield"
    assert started_audits == [audit_id]


def test_openrouter_uses_free_router_and_never_uses_an_openai_endpoint(
    monkeypatch,
):
    observed = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            observed["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, url, *, headers, json):
            observed["url"] = url
            observed["headers"] = headers
            observed["body"] = json
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json_lib.dumps(
                                    {
                                        "project_name": "Actual Homes",
                                        "builder": "Actual Group",
                                        "city": "Bengaluru",
                                    }
                                )
                            }
                        }
                    ]
                },
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(main.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key-not-real")
    monkeypatch.setattr(main, "OPENROUTER_MODEL", "openrouter/free")

    result = __import__("asyncio").run(
        main.llm_extract_claims("audit-id", "Project Actual Homes by Actual Group.")
    )

    assert result["project_name"] == "Actual Homes"
    assert result["builder"] == "Actual Group"
    assert observed["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert observed["headers"]["Authorization"] == "Bearer test-key-not-real"
    assert observed["body"]["model"] == "openrouter/free"
    assert result["raw_excerpt"] == ""


def test_full_manual_audit_pipeline_returns_evidence_and_scored_findings(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    monkeypatch.setattr(main, "TINYFISH_API_KEY", "test-key")
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key")

    async def fake_agent_request(audit_id, goal, url, *args, **kwargs):
        return {
            "found": True,
            "rera_id": "PRM-KA-123",
            "project_name": "Example Homes",
            "promoter": "Example Group",
            "status": "Active",
            "original_completion_date": "2026-01-01",
            "revised_completion_date": "2028-01-01",
            "extensions_count": 2,
            "complaints_count": 1,
            "project_url": "https://rera.karnataka.gov.in/project/123",
            "run_id": "mock-rera-run",
        }

    async def fake_extract_listing(audit_id, listing_url):
        assert listing_url == "https://listings.example/project"
        return {
            "property_address": (
                "Plot 14, Whitefield Main Road, Whitefield, Bengaluru"
            ),
            "locality": "Whitefield",
        }

    async def fake_gather_evidence(
        audit_id, project_name, builder, locality, city, property_address=None
    ):
        assert locality == "Whitefield"
        assert property_address == (
            "Plot 14, Whitefield Main Road, Whitefield, Bengaluru"
        )
        return {
            "news": [{
                "title": "Project article",
                "url": "https://news.example/project",
                "excerpt": "Construction schedule updated.",
            }],
            "residents": [{
                "title": "Resident thread",
                "url": "https://reddit.com/r/city/post",
                "excerpt": "Residents discuss the project.",
            }],
            "locality": [{
                "title": "Locality report",
                "url": "https://news.example/locality",
                "excerpt": "Roads flooded during heavy rain.",
                "source_type": "news",
                "source_scope": "locality",
            }],
            "failed_agents": 0,
            "research_status": {
                "news": {"status": "completed", "count": 1, "message": "1 result."},
                "residents": {"status": "completed", "count": 1, "message": "1 result."},
                "locality": {"status": "completed", "count": 1, "message": "1 result."},
                "classification": {
                    "status": "completed",
                    "message": "Exact source quotes were checked.",
                },
            },
        }

    async def fake_collect_builder_projects(*args):
        return {
            "projects": [{
                "project_name": "Example Homes Phase 2",
                "promoter": "Example Group",
                "rera_id": "PRM-KA-456",
                "status": "Active",
                "extensions_count": 0,
                "complaints_count": 0,
                "project_url": "https://rera.karnataka.gov.in/project/456",
                "public_sources": [{
                    "title": "Example Homes Phase 2 reviews",
                    "url": "https://reviews.example/phase-2",
                }],
                "retrieved_at": "2026-11-01T00:00:00Z",
                "run_id": "mock-builder-run",
            }],
            "message": "Verified one builder project.",
        }

    monkeypatch.setattr(main, "agent_request", fake_agent_request)
    monkeypatch.setattr(main, "extract_listing", fake_extract_listing)
    monkeypatch.setattr(main, "gather_evidence", fake_gather_evidence)
    monkeypatch.setattr(main, "collect_builder_projects", fake_collect_builder_projects)

    with TestClient(main.app) as client:
        created = client.post(
            "/api/audits",
            json={
                "project_name": "Example Homes",
                "builder": "Example Group",
                "city": "Bengaluru",
                "listing_url": "https://listings.example/project",
                "possession_date": "2026-01-01",
                "rera_id": "PRM-KA-123",
            },
        )
        assert created.status_code == 202
        audit_id = created.json()["id"]

        for _ in range(50):
            audit = client.get(f"/api/audits/{audit_id}").json()
            if audit["status"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.02)

    assert audit["status"] == "completed"
    assert audit["locality_name"] == "Whitefield"
    assert audit["claims"]["locality"] == "Whitefield"
    assert audit["claims"]["property_address"] == (
        "Plot 14, Whitefield Main Road, Whitefield, Bengaluru"
    )
    assert audit["rera_record"]["run_id"] == "mock-rera-run"
    assert audit["score"]["score"] == 56
    assert audit["score"]["confidence"] == "High"
    assert len(audit["news"]) == 1
    assert len(audit["residents"]) == 1
    assert len(audit["locality"]) == 1
    assert len(audit["builder_projects"]) == 1
    assert audit["builder_projects"][0]["public_sources"][0]["url"] == (
        "https://reviews.example/phase-2"
    )
    assert audit["research_status"]["builder"]["status"] == "completed"
    assert any(item["type"] == "possession_mismatch" for item in audit["findings"])


def test_monitor_webhook_compares_and_advances_baseline(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "test-webhook-secret")
    baseline = {"results": [{"url": "https://rera.example/project", "text": "old"}]}
    audit = {
        "id": "audit-watch",
        "created_at": "2026-11-01T00:00:00Z",
        "project_name": "Example Homes",
        "builder": "Example Group",
    }

    with TestClient(main.app) as client:
        database.save_audit(audit)
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO watches (id, audit_id, monitor_id, type, target, cron, baseline_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "watch-id",
                    "audit-watch",
                    "monitor-id",
                    "fetch",
                    "https://rera.example/project",
                    "0 9 * * *",
                    json_lib.dumps(baseline),
                ),
            )
        unauthorized = client.post(
            "/api/webhooks/tinyfish",
            json={"monitor": {"id": "monitor-id"}, "run": baseline},
        )
        changed = client.post(
            "/api/webhooks/tinyfish?secret=test-webhook-secret",
            json={
                "monitor": {"id": "monitor-id"},
                "run": {"results": [{"url": "https://rera.example/project", "text": "new"}]},
            },
        )
        unchanged = client.post(
            "/api/webhooks/tinyfish?secret=test-webhook-secret",
            json={
                "monitor": {"id": "monitor-id"},
                "run": {"results": [{"url": "https://rera.example/project", "text": "new"}]},
            },
        )
        watches = client.get("/api/watches").json()

    assert unauthorized.status_code == 401
    assert changed.status_code == 200
    assert unchanged.status_code == 200
    assert watches[0]["last_change_json"]["run"]["results"][0]["text"] == "new"
    assert watches[0]["baseline_json"]["results"][0]["text"] == "new"


def test_watch_route_creates_rera_and_news_monitors(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    monkeypatch.setattr(main, "PUBLIC_BASE_URL", "https://api.example")
    monkeypatch.setattr(main, "WEBHOOK_SECRET", "not-a-real-secret")

    async def fake_tinyfish_call(
        audit_id, tool, input_summary, target_url, request
    ):
        monitor_type = "fetch" if target_url.startswith("https://rera") else "search"
        return {
            "monitor": {"id": f"{monitor_type}-monitor"},
            "run": {"results": [{"text": f"{monitor_type} baseline"}]},
        }

    monkeypatch.setattr(main, "tinyfish_call", fake_tinyfish_call)

    with TestClient(main.app) as client:
        database.save_audit(
            {
                "id": "watch-audit",
                "created_at": "2026-11-01T00:00:00Z",
                "project_name": "Example Homes",
                "builder": "Example Group",
                "rera_record": {
                    "project_url": "https://rera.karnataka.gov.in/project/123"
                },
            }
        )
        response = client.post(
            "/api/watches",
            json={"audit_id": "watch-audit", "schedule_cron": "0 9 * * *"},
        )
        watchlist = client.get("/api/watches").json()

    assert response.status_code == 201
    assert {monitor["type"] for monitor in response.json()["monitors"]} == {
        "fetch",
        "search",
    }
    assert len(watchlist) == 2
    assert all("not-a-real-secret" not in watch["target"] for watch in watchlist)


def test_extracted_unsupported_state_stops_before_portal_search(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")

    async def unsupported_listing(*args):
        return {
            "project_name": "Example Homes",
            "builder": "Example Group",
            "city": "Pune",
            "state": "Maharashtra",
        }

    async def should_not_search(*args, **kwargs):
        raise AssertionError("Unsupported state must not start a portal search.")

    monkeypatch.setattr(main, "extract_listing", unsupported_listing)
    monkeypatch.setattr(main, "agent_request", should_not_search)
    monkeypatch.setattr(main, "gather_evidence", should_not_search)
    monkeypatch.setattr(main, "collect_builder_projects", should_not_search)

    with TestClient(main.app) as client:
        created = client.post(
            "/api/audits",
            json={"listing_url": "https://nobroker.in/listing/example"},
        )
        audit_id = created.json()["id"]
        for _ in range(50):
            audit = client.get(f"/api/audits/{audit_id}").json()
            if audit["status"] == "unsupported":
                break
            time.sleep(0.02)
        runs = client.get(f"/api/audits/{audit_id}/runs").json()

    assert audit["status"] == "unsupported"
    assert audit["state"] == "Maharashtra"
    assert audit["score"]["score"] is None
    assert audit["research_status"]["locality"]["status"] == "skipped"
    assert runs == []


def test_openrouter_classification_requires_verbatim_source_excerpt(monkeypatch):
    news = [{
        "url": "https://news.example/project",
        "_source_text": "The Example Homes project was delayed by six months.",
    }]
    residents = [{
        "url": "https://reddit.com/r/city/post",
        "_source_text": "Residents report frequent power cuts.",
    }]

    async def fake_openrouter(system_prompt, user_content, timeout_seconds=60):
        sources = json_lib.loads(user_content)["sources"]
        assert {source["source_type"] for source in sources} == {
            "news",
            "resident_forum",
        }
        return {
            "items": [
                {
                    "url": news[0]["url"],
                    "source_type": "news",
                    "relevant": True,
                    "negative": True,
                    "summary": "The project was delayed by six months.",
                    "excerpt": "The Example Homes project was delayed by six months.",
                },
                {
                    "url": residents[0]["url"],
                    "source_type": "resident_forum",
                    "relevant": True,
                    "issue_category": "power",
                    "excerpt": "Residents report a complete power failure.",
                },
            ]
        }

    monkeypatch.setattr(main, "openrouter_chat_json", fake_openrouter)

    __import__("asyncio").run(
        main.classify_sources(
            news,
            residents,
            subject={"project_name": "Example Homes", "builder": "Example Group"},
        )
    )

    assert news[0]["classification"]["verified"] is True
    assert news[0]["classification"]["negative"] is True
    assert news[0]["classification"]["summary"] == "The project was delayed by six months."
    assert residents[0]["classification"]["verified"] is False
    assert residents[0]["classification"]["excerpt"] == ""


def test_locality_research_searches_local_news_and_forums(monkeypatch):
    observed_queries = []
    observed_subject = {}

    async def fake_search(audit_id, query, domain_type="web", include_domains=None):
        observed_queries.append((query, domain_type, include_domains))
        index = len(observed_queries)
        return {
            "results": [{
                "title": f"Result {index}",
                "url": f"https://source.example/{index}",
                "snippet": "Whitefield has flooded roads after heavy rain.",
            }],
            "trueestate_trace_id": f"search-{index}",
        }

    async def fake_fetch(audit_id, urls, purpose):
        return {
            "trueestate_trace_id": "fetch-1",
            "results": [
                {"url": url, "text": "Whitefield has flooded roads after heavy rain."}
                for url in urls
            ],
        }

    async def fake_classify(news, residents, locality, subject):
        assert subject["locality"] is None
        observed_subject.update(subject)
        assert len(locality) == 4
        for item in news + residents + locality:
            item["classification"] = {
                "relevant": True,
                "negative": False,
                "issue_category": "waterlogging",
                "topic": "Road flooding",
                "excerpt": "Whitefield has flooded roads after heavy rain.",
                "verified": True,
            }

    monkeypatch.setattr(main, "search_web", fake_search)
    monkeypatch.setattr(main, "fetch_content", fake_fetch)
    monkeypatch.setattr(main, "classify_sources", fake_classify)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key-not-real")

    result = __import__("asyncio").run(
        main.gather_evidence(
            "audit-id",
            "Example Homes",
            "Example Group",
            None,
            "Bengaluru",
            "Plot 14, Whitefield Main Road, Bengaluru",
        )
    )

    assert len(observed_queries) == 7
    assert any(query[1] == "news" and "Whitefield" in query[0] for query in observed_queries)
    assert any(query[2] == "reddit.com" and "Whitefield" in query[0] for query in observed_queries)
    assert any(query[2] is None and "forum" in query[0] for query in observed_queries)
    assert result["research_status"]["locality"]["status"] == "completed"
    assert result["locality"][0]["classification"]["verified"] is True
    assert "Whitefield" in observed_queries[-1][0]
    assert "Plot 14" not in observed_queries[-1][0]
    assert observed_subject["property_address"] == "Plot 14, Whitefield Main Road, Bengaluru"
    assert result["failed_agents"] == 0


def test_evidence_fetch_splits_requests_at_tinyfish_limit(monkeypatch):
    fetch_batch_sizes = []

    async def fake_search(audit_id, query, domain_type="web", include_domains=None):
        index = len(search_calls)
        search_calls.append(query)
        return {
            "results": [
                {
                    "title": f"Whitefield Example Homes report {index}-{result_index}",
                    "url": f"https://source-{index}-{result_index}.example/article",
                    "snippet": "Whitefield Example Homes public source.",
                }
                for result_index in range(3)
            ]
        }

    async def fake_fetch(audit_id, urls, purpose):
        fetch_batch_sizes.append(len(urls))
        assert len(urls) <= 10
        return {
            "results": [{"url": url, "text": "Whitefield Example Homes source text."} for url in urls]
        }

    async def fake_classify(news, residents, locality, subject):
        for item in news + residents + locality:
            item["classification"] = {
                "relevant": True,
                "negative": False,
                "issue_category": None,
                "topic": "Public coverage",
                "summary": "Whitefield Example Homes source text.",
                "excerpt": "Whitefield Example Homes source text.",
                "verified": True,
            }

    search_calls = []
    monkeypatch.setattr(main, "search_web", fake_search)
    monkeypatch.setattr(main, "fetch_content", fake_fetch)
    monkeypatch.setattr(main, "classify_sources", fake_classify)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key-not-real")

    result = __import__("asyncio").run(
        main.gather_evidence(
            "audit-id",
            "Example Homes",
            "Example Group",
            "Whitefield",
            "Bengaluru",
        )
    )

    assert len(search_calls) == 7
    assert fetch_batch_sizes == [10, 8]
    assert len(result["news"]) + len(result["residents"]) + len(result["locality"]) == 18
    assert result["failed_agents"] == 0


def test_locality_classifier_rejects_city_only_matches(monkeypatch):
    city_only_source = {
        "url": "https://news.example/bengaluru",
        "title": "Bengaluru roads flooded after rain",
        "source_type": "news",
        "source_scope": "locality",
        "_source_text": "Bengaluru roads flooded after rain.",
    }
    address_match = {
        "url": "https://news.example/whitefield",
        "title": "Whitefield roads flooded after rain",
        "source_type": "news",
        "source_scope": "locality",
        "_source_text": "Roads near Whitefield Main Road flooded after rain.",
    }

    async def fake_openrouter(system_prompt, user_content, timeout_seconds=60):
        return {
            "items": [
                {
                    "url": city_only_source["url"],
                    "source_type": "news",
                    "relevant": True,
                    "summary": "Bengaluru roads flooded after rain.",
                    "excerpt": "Bengaluru roads flooded after rain.",
                },
                {
                    "url": address_match["url"],
                    "source_type": "news",
                    "relevant": True,
                    "summary": "Roads near Whitefield flooded after rain.",
                    "excerpt": "Roads near Whitefield Main Road flooded after rain.",
                },
            ]
        }

    monkeypatch.setattr(main, "openrouter_chat_json", fake_openrouter)

    __import__("asyncio").run(
        main.classify_sources(
            [],
            [],
            [city_only_source, address_match],
            {
                "locality": None,
                "property_address": "Plot 14, Whitefield Main Road, Bengaluru",
                "city": "Bengaluru",
            },
        )
    )

    assert city_only_source["classification"]["relevant"] is False
    assert city_only_source["classification"]["verified"] is False
    assert city_only_source["classification"]["excerpt"] == ""
    assert address_match["classification"]["relevant"] is True
    assert address_match["classification"]["verified"] is True


def test_rera_results_require_verified_official_registration():
    assert main._rera_validation_error({"found": False}) == (
        "No official project record was verified; non-registration is unknown."
    )
    assert main._rera_validation_error({
        "found": True,
        "rera_id": "PRM-KA-123",
        "project_url": "https://rera.karnataka.gov.in.evil.example/project",
    })
    assert main._rera_validation_error({
        "found": True,
        "rera_id": "PRM-KA-123",
        "project_url": "https://rera.karnataka.gov.in/project/123",
    }) is None


def test_rera_lookup_uses_one_portal_field_then_searches_and_retries(monkeypatch):
    observed_agent_goals = []
    search_queries = []
    fetch_calls = []
    portal_attempts = 0

    async def fake_agent_request(
        audit_id, goal, url, output_schema=None, browser_profile="lite"
    ):
        nonlocal portal_attempts
        assert url == main.KARNATAKA_RERA_URL
        observed_agent_goals.append(goal)
        portal_attempts += 1
        if portal_attempts == 1:
            return {
                "found": False,
                "status": "Portal search could not be completed because of a timeout.",
            }
        return {
            "found": True,
            "project_name": "Example Homes",
            "promoter": "Example Group",
            "rera_id": "PRM-KA-123",
            "status": "Active",
            "original_completion_date": "2027-01-01",
            "revised_completion_date": "2027-01-01",
            "extensions_count": 0,
            "complaints_count": 0,
            "project_url": url,
            "run_id": "fallback-detail-run",
        }

    async def fake_search(audit_id, query, domain_type="web", include_domains=None):
        search_queries.append((query, domain_type, include_domains))
        return {"results": [{
            "url": "https://example.com/property",
            "title": "Example Homes by Example Group in Whitefield",
            "snippet": "Karnataka RERA registration PRM-KA-123",
        }]}

    async def fake_fetch_content(audit_id, urls, purpose):
        fetch_calls.append((urls, purpose))
        return {"results": [{
            "url": "https://example.com/property",
            "text": (
                "Example Homes by Example Group, Whitefield, Bengaluru. "
                "Karnataka RERA registration PRM-KA-123."
            ),
        }]}

    async def fake_openrouter(system_prompt, user_content, timeout_seconds=60):
        assert "do not guess" in system_prompt.casefold()
        assert "Whitefield" in user_content
        return {
            "project_name": "Example Homes",
            "rera_id": "PRM-KA-123",
            "source_url": "https://example.com/property",
        }

    monkeypatch.setattr(main, "agent_request", fake_agent_request)
    monkeypatch.setattr(main, "search_web", fake_search)
    monkeypatch.setattr(main, "fetch_content", fake_fetch_content)
    monkeypatch.setattr(main, "openrouter_chat_json", fake_openrouter)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key")

    result = __import__("asyncio").run(
        main.lookup_rera_project(
            "audit-id",
            "Example Homes",
            "Example Group",
            "Whitefield",
            "Bengaluru",
            "PRM-KA-123",
            {"property_address": "Whitefield Main Road, Bengaluru"},
        )
    )

    assert result["found"] is True
    assert result["run_id"] == "fallback-detail-run"
    assert portal_attempts == 2
    assert len(search_queries) == 1
    assert "Example Homes" in search_queries[0][0]
    assert "Example Group" in search_queries[0][0]
    assert "Whitefield Main Road, Bengaluru" in search_queries[0][0]
    assert len(fetch_calls) == 1
    assert '"Registration ID" search field' in observed_agent_goals[0]
    assert "Leave every other search field empty" in observed_agent_goals[0]
    assert '"Project Name" search field' not in observed_agent_goals[0]
    assert '"Registration ID" search field' in observed_agent_goals[1]


def test_rera_lookup_uses_project_name_field_when_no_registration_id(monkeypatch):
    observed_goals = []

    async def fake_agent_request(
        audit_id, goal, url, output_schema=None, browser_profile="lite"
    ):
        observed_goals.append(goal)
        return {
            "found": True,
            "project_name": "Example Homes",
            "promoter": "Example Group",
            "rera_id": "PRM-KA-123",
            "status": "Active",
            "project_url": "https://rera.karnataka.gov.in/project/123",
        }

    monkeypatch.setattr(main, "agent_request", fake_agent_request)

    result = __import__("asyncio").run(
        main.lookup_rera_project(
            "audit-id",
            "Example Homes",
            "Example Group",
            "Whitefield",
            "Bengaluru",
            None,
        )
    )

    assert result["found"] is True
    assert len(observed_goals) == 1
    assert '"Project Name" search field' in observed_goals[0]
    assert "exactly \"Example Homes\"" in observed_goals[0]
    assert "Leave every other search field empty" in observed_goals[0]
    assert '"Registration ID" search field' not in observed_goals[0]


def test_rera_lookup_preserves_portal_failure_in_unknown_record(monkeypatch):
    portal_attempts = 0
    search_attempts = 0

    async def fake_agent_request(*args, **kwargs):
        nonlocal portal_attempts
        portal_attempts += 1
        return {
            "found": False,
            "status": "Portal inaccessible: ERR_TUNNEL_CONNECTION_FAILED.",
        }

    async def fake_search(*args, **kwargs):
        nonlocal search_attempts
        search_attempts += 1
        return {"results": []}

    monkeypatch.setattr(main, "agent_request", fake_agent_request)
    monkeypatch.setattr(main, "search_web", fake_search)
    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "")

    result = __import__("asyncio").run(
        main.lookup_rera_project(
            "audit-id",
            "Example Homes",
            "Example Group",
            "Whitefield",
            "Bengaluru",
            "PRM-KA-123",
        )
    )

    assert result["found"] is False
    assert "ERR_TUNNEL_CONNECTION_FAILED" in result["source_error"]
    assert "three portal attempts" in result["source_error"]
    assert portal_attempts == 3
    assert search_attempts == 2


def test_builder_portfolio_collects_rera_details_and_matching_public_views(monkeypatch):
    observed_profiles = []

    async def fake_agent_request(
        audit_id, goal, url, output_schema=None, browser_profile="lite"
    ):
        observed_profiles.append(browser_profile)
        if url == main.KARNATAKA_RERA_URL:
            return {
                "projects": [{
                    "project_name": "Example Homes Phase 2",
                    "promoter": "Example Group",
                    "rera_id": "PRM-KA-456",
                    "project_url": "https://rera.karnataka.gov.in/project/456",
                }]
            }
        return {
            "found": True,
            "project_name": "Example Homes Phase 2",
            "promoter": "Example Group",
            "rera_id": "PRM-KA-456",
            "status": "Active",
            "original_completion_date": "2025-01-01",
            "revised_completion_date": "2026-01-01",
            "extensions_count": 1,
            "complaints_count": 2,
            "project_url": url,
            "run_id": "project-run-456",
        }

    async def fake_search(audit_id, query, domain_type="web", include_domains=None):
        assert '"Example Homes Phase 2"' in query
        return {
            "results": [
                {
                    "title": "Example Homes Phase 2 buyer reviews",
                    "url": "https://reviews.example/phase-2",
                    "site_name": "HomeReviews",
                    "snippet": "Reviews for Example Homes Phase 2.",
                },
                {
                    "title": "Bengaluru apartment reviews",
                    "url": "https://reviews.example/unrelated",
                    "snippet": "Reviews for another project.",
                },
            ]
        }

    monkeypatch.setattr(main, "agent_request", fake_agent_request)
    monkeypatch.setattr(main, "search_web", fake_search)

    result = __import__("asyncio").run(
        main.collect_builder_projects(
            "audit-id", "Example Group", "Bengaluru", "PRM-KA-123"
        )
    )

    assert len(result["projects"]) == 1
    project = result["projects"][0]
    assert project["status"] == "Active"
    assert project["extensions_count"] == 1
    assert project["complaints_count"] == 2
    assert project["run_id"] == "project-run-456"
    assert project["public_sources"] == [{
        "title": "Example Homes Phase 2 buyer reviews",
        "url": "https://reviews.example/phase-2",
        "source_name": "HomeReviews",
        "snippet": "Reviews for Example Homes Phase 2.",
    }]
    assert observed_profiles == ["stealth", "stealth"]
    assert "1 matching public reference" in result["message"]


def test_incomplete_rera_automation_stays_unknown_not_unregistered(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "trueestate.db")
    monkeypatch.setattr(main, "TINYFISH_API_KEY", "test-key-not-real")

    async def malformed_rera(*args, **kwargs):
        return {"status": "Active"}

    async def empty_evidence(*args, **kwargs):
        return {
            "news": [],
            "residents": [],
            "locality": [],
            "failed_agents": 0,
            "research_status": {
                key: {"status": "completed", "count": 0, "message": "No sources."}
                for key in ("news", "residents", "locality", "classification")
            },
        }

    async def empty_projects(*args):
        return {
            "projects": [],
            "message": "Builder search completed; no additional verifiable RERA projects were returned.",
        }

    async def empty_search(*args, **kwargs):
        return {"results": []}

    monkeypatch.setattr(main, "agent_request", malformed_rera)
    monkeypatch.setattr(main, "search_web", empty_search)
    monkeypatch.setattr(main, "gather_evidence", empty_evidence)
    monkeypatch.setattr(main, "collect_builder_projects", empty_projects)

    with TestClient(main.app) as client:
        created = client.post(
            "/api/audits",
            json={
                "project_name": "Example Homes",
                "builder": "Example Group",
                "city": "Bengaluru",
            },
        )
        audit_id = created.json()["id"]
        for _ in range(50):
            audit = client.get(f"/api/audits/{audit_id}").json()
            if audit["status"] == "partial":
                break
            time.sleep(0.02)

    assert audit["status"] == "partial"
    assert "RERA automation returned no valid found flag." in (
        audit["rera_record"]["source_error"]
    )
    assert audit["score"]["score"] is None
    assert all(
        finding["type"] != "registration_status"
        for finding in audit["score"]["deductions"]
    )


def test_listing_claim_extraction_captures_full_address_and_locality(monkeypatch):
    observed_prompt = {}

    async def fake_openrouter(system_prompt, user_content, timeout_seconds=60):
        observed_prompt["system"] = system_prompt
        observed_prompt["input"] = user_content
        return {
            "project_name": "Example Homes",
            "builder": "Example Group",
            "property_address": "Plot 14, Whitefield Main Road, Whitefield, Bengaluru",
            "locality": "Whitefield",
            "city": "Bengaluru",
            "state": "Karnataka",
        }

    monkeypatch.setattr(main, "openrouter_chat_json", fake_openrouter)

    result = __import__("asyncio").run(
        main.llm_extract_claims(
            "audit-id",
            "Address: Plot 14, Whitefield Main Road, Whitefield, Bengaluru",
        )
    )

    assert result["property_address"].startswith("Plot 14, Whitefield Main Road")
    assert result["locality"] == "Whitefield"
    assert "complete property address" in observed_prompt["system"]


def test_listing_extraction_uses_browser_fallback_to_fill_missing_claims(
    monkeypatch,
):
    observed_agent = []

    async def fake_fetch_content(audit_id, urls, purpose):
        assert urls == ["https://listing.example/property"]
        return {"results": [{"text": "Mahendra Arto Helix in Neeladri Nagar, Bangalore"}]}

    async def fake_llm_extract_claims(audit_id, page_text):
        return {
            "project_name": "Mahendra Arto Helix",
            "builder": None,
            "property_address": "Phase 1 Electronics City, Bangalore",
            "locality": "Neeladri Nagar",
            "city": "Bangalore",
            "state": "Karnataka",
        }

    async def fake_agent_request(
        audit_id, goal, url, output_schema=None, browser_profile="lite"
    ):
        observed_agent.append((goal, url, browser_profile))
        return {
            "project_name": "Different guessed project",
            "builder": "Mahendra Homes",
            "city": "Different guessed city",
            "price": "₹1.67 Cr",
        }

    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(main, "TINYFISH_API_KEY", "test-key")
    monkeypatch.setattr(main, "fetch_content", fake_fetch_content)
    monkeypatch.setattr(main, "llm_extract_claims", fake_llm_extract_claims)
    monkeypatch.setattr(main, "agent_request", fake_agent_request)

    result = __import__("asyncio").run(
        main.extract_listing("audit-id", "https://listing.example/property")
    )

    assert result["project_name"] == "Mahendra Arto Helix"
    assert result["builder"] == "Mahendra Homes"
    assert result["city"] == "Bangalore"
    assert result["locality"] == "Neeladri Nagar"
    assert result["price"] == "₹1.67 Cr"
    assert observed_agent[0][1:] == (
        "https://listing.example/property",
        "stealth",
    )


def test_listing_extraction_preserves_partial_claims_when_browser_fallback_fails(
    monkeypatch,
):
    async def fake_fetch_content(audit_id, urls, purpose):
        return {"results": [{"text": "Mahendra Arto Helix listing"}]}

    async def fake_llm_extract_claims(audit_id, page_text):
        return {
            "project_name": "Mahendra Arto Helix",
            "builder": None,
            "city": "Bangalore",
        }

    async def fake_agent_request(*args, **kwargs):
        raise main.IntegrationError("Browser automation timed out.")

    monkeypatch.setattr(main, "OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(main, "TINYFISH_API_KEY", "test-key")
    monkeypatch.setattr(main, "fetch_content", fake_fetch_content)
    monkeypatch.setattr(main, "llm_extract_claims", fake_llm_extract_claims)
    monkeypatch.setattr(main, "agent_request", fake_agent_request)

    result = __import__("asyncio").run(
        main.extract_listing("audit-id", "https://listing.example/property")
    )

    assert result["project_name"] == "Mahendra Arto Helix"
    assert result["builder"] is None
    assert result["city"] == "Bangalore"
    assert "Browser automation timed out" in result["extraction_error"]
