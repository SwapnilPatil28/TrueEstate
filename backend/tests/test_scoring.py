from app.scoring import calculate_score, compare_claims, evidence_findings


def test_possession_mismatch_uses_severity_thresholds():
    claims = {"possession_date": "2026-01-15"}
    record = {
        "found": True,
        "revised_completion_date": "2027-01-15",
        "project_url": "https://rera.karnataka.gov.in/project",
        "run_id": "run-1",
    }

    findings = compare_claims(claims, record, "2026-01-01T00:00:00Z")

    assert len(findings) == 1
    assert findings[0]["severity"] == "warning"
    assert findings[0]["points"] == 15
    assert findings[0]["source_url"] == record["project_url"]
    assert findings[0]["run_id"] == "run-1"


def test_unregistered_project_and_extensions_are_deterministic_deductions():
    record = {
        "found": False,
        "status": "Revoked",
        "extensions_count": 3,
        "complaints_count": 8,
        "project_url": "https://rera.karnataka.gov.in/project",
        "retrieved_at": "2026-01-01T00:00:00Z",
        "run_id": "run-2",
    }
    findings = compare_claims({}, record, "2026-01-01T00:00:00Z")

    result = calculate_score(record, findings)

    assert result["score"] == 25
    assert result["risk_band"] == "Critical"
    assert result["confidence"] == "Low"
    assert {item["type"] for item in result["deductions"]} == {
        "registration_status",
        "deadline_extensions",
        "registered_complaints",
    }


def test_missing_record_does_not_make_an_unverified_score_deduction():
    result = calculate_score(None, [], failed_agents=1)

    assert result["score"] is None
    assert result["risk_band"] == "Unknown"
    assert result["confidence"] == "Low"
    assert result["deductions"] == []


def test_ambiguous_rera_candidates_do_not_receive_a_trust_score():
    record = {"found": False, "ambiguous": True, "candidates": [{}, {}]}

    result = calculate_score(record, [])

    assert result["score"] is None
    assert result["risk_band"] == "Unknown"
    assert result["confidence"] == "Low"


def test_unverified_rera_payload_does_not_show_a_clean_score():
    result = calculate_score({"found": True, "status": "Active"}, [])

    assert result["score"] is None
    assert result["risk_band"] == "Unknown"
    assert result["confidence"] == "Low"


def test_possession_threshold_uses_calendar_months():
    warning = compare_claims(
        {"possession_date": "2026-01-31"},
        {"found": True, "revised_completion_date": "2026-08-01"},
        "2026-01-01T00:00:00Z",
    )
    exact_six_months = compare_claims(
        {"possession_date": "2026-01-31"},
        {"found": True, "revised_completion_date": "2026-07-31"},
        "2026-01-01T00:00:00Z",
    )

    assert warning[0]["points"] == 15
    assert exact_six_months == []


def test_claimed_rera_id_is_normalized_before_comparison():
    findings = compare_claims(
        {"rera_id": "PRM/KA/RERA/1251/310/PR/190101/002222"},
        {"found": True, "rera_id": "PRM-KA-RERA-9999"},
        "2026-01-01T00:00:00Z",
    )

    assert any(item["type"] == "rera_id_mismatch" for item in findings)


def test_one_resident_post_is_not_scored_as_a_verified_livability_issue():
    residents = [
        {
            "url": "https://reddit.com/r/city/post-1",
            "classification": {
                "relevant": True,
                "issue_category": "waterlogging",
                "verified": True,
                "excerpt": "The basement flooded after rain.",
            },
        }
    ]

    assert evidence_findings([], residents, "2026-01-01T00:00:00Z") == []


def test_two_independent_resident_posts_require_verified_source_quotes():
    residents = [
        {
            "url": "https://reddit.com/r/city/post-1",
            "classification": {
                "relevant": True,
                "issue_category": "waterlogging",
                "verified": True,
                "excerpt": "The basement flooded after rain.",
            },
        },
        {
            "url": "https://reddit.com/r/city/post-2",
            "classification": {
                "relevant": True,
                "issue_category": "waterlogging",
                "verified": False,
                "excerpt": "",
            },
        },
        {
            "url": "https://reddit.com/r/city/post-3",
            "classification": {
                "relevant": True,
                "issue_category": "waterlogging",
                "verified": True,
                "excerpt": "Water entered our parking area.",
            },
        },
    ]

    findings = evidence_findings([], residents, "2026-01-01T00:00:00Z")

    assert len(findings) == 1
    assert findings[0]["points"] == 10
    assert len(findings[0]["evidence_urls"]) == 2


def test_negative_news_requires_relevance_and_a_verified_quote():
    negative_article = {
        "url": "https://news.example/article",
        "classification": {
            "relevant": True,
            "negative": True,
            "verified": True,
            "excerpt": "The project completion was delayed.",
        },
    }
    unrelated_article = {
        "url": "https://news.example/unrelated",
        "classification": {
            "relevant": False,
            "negative": True,
            "verified": True,
            "excerpt": "A different builder faced a delay.",
        },
    }

    findings = evidence_findings(
        [unrelated_article, negative_article], [], "2026-01-01T00:00:00Z"
    )

    assert len(findings) == 1
    assert findings[0]["type"] == "negative_news"
    assert findings[0]["source_url"] == negative_article["url"]


def test_locality_news_does_not_create_a_builder_negative_news_deduction():
    locality_article = {
        "url": "https://news.example/locality",
        "source_type": "news",
        "source_scope": "locality",
        "classification": {
            "relevant": True,
            "negative": True,
            "verified": True,
            "excerpt": "Several roads in the neighborhood flooded.",
        },
    }

    assert evidence_findings([], [], "2026-01-01T00:00:00Z", [locality_article]) == []


def test_two_locality_forum_threads_create_cited_livability_finding():
    locality_threads = [
        {
            "url": "https://reddit.com/r/city/post-1",
            "source_type": "resident_forum",
            "source_scope": "locality",
            "classification": {
                "relevant": True,
                "issue_category": "flooding",
                "verified": True,
                "excerpt": "Water covered the road after the rain.",
            },
        },
        {
            "url": "https://reddit.com/r/city/post-2",
            "source_type": "resident_forum",
            "source_scope": "locality",
            "classification": {
                "relevant": True,
                "issue_category": "flooding",
                "verified": True,
                "excerpt": "The junction was flooded for hours.",
            },
        },
    ]

    findings = evidence_findings(
        [], [], "2026-01-01T00:00:00Z", locality_threads
    )

    assert len(findings) == 1
    assert findings[0]["points"] == 10
    assert findings[0]["evidence_urls"] == [
        "https://reddit.com/r/city/post-1",
        "https://reddit.com/r/city/post-2",
    ]


def test_builder_history_score_uses_only_checked_external_project_records():
    projects = [
        {
            "status": "Active",
            "extensions_count": 1,
            "project_url": "https://rera.karnataka.gov.in/project/1",
            "run_id": "builder-run-1",
        },
        {
            "status": "Active",
            "extensions_count": 0,
            "project_url": "https://rera.karnataka.gov.in/project/2",
            "run_id": "builder-run-2",
        },
    ]

    result = calculate_score(
        {"found": True, "rera_id": "primary", "status": "Active"},
        [],
        builder_projects=projects,
    )

    deduction = next(
        item for item in result["deductions"] if item["type"] == "builder_track_record"
    )
    assert deduction["points"] == 8
    assert len(deduction["evidence_urls"]) == 1
    assert deduction["run_id"] == "builder-run-1"
