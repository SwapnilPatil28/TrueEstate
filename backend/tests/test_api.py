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

    async def fake_gather_evidence(audit_id, project_name, builder, city):
        return (
            [{
                "title": "Project article",
                "url": "https://news.example/project",
                "excerpt": "Construction schedule updated.",
            }],
            [{
                "title": "Resident thread",
                "url": "https://reddit.com/r/city/post",
                "excerpt": "Residents discuss the project.",
            }],
            0,
            True,
            True,
            None,
        )

    async def fake_collect_builder_projects(*args):
        return [{
            "project_name": "Example Homes Phase 2",
            "promoter": "Example Group",
            "rera_id": "PRM-KA-456",
            "status": "Active",
            "extensions_count": 0,
            "complaints_count": 0,
            "project_url": "https://rera.karnataka.gov.in/project/456",
            "retrieved_at": "2026-11-01T00:00:00Z",
            "run_id": "mock-builder-run",
        }]

    monkeypatch.setattr(main, "agent_request", fake_agent_request)
    monkeypatch.setattr(main, "gather_evidence", fake_gather_evidence)
    monkeypatch.setattr(main, "collect_builder_projects", fake_collect_builder_projects)

    with TestClient(main.app) as client:
        created = client.post(
            "/api/audits",
            json={
                "project_name": "Example Homes",
                "builder": "Example Group",
                "city": "Bengaluru",
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
    assert audit["rera_record"]["run_id"] == "mock-rera-run"
    assert audit["score"]["score"] == 56
    assert audit["score"]["confidence"] == "High"
    assert len(audit["news"]) == 1
    assert len(audit["residents"]) == 1
    assert len(audit["builder_projects"]) == 1
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
        sources = json_lib.loads(user_content)
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

    __import__("asyncio").run(main.classify_sources(news, residents))

    assert news[0]["classification"]["verified"] is True
    assert news[0]["classification"]["negative"] is True
    assert residents[0]["classification"]["verified"] is False
    assert residents[0]["classification"]["excerpt"] == ""
