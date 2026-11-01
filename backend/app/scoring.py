import re
import calendar
from datetime import date, datetime
from typing import Any


def normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _parse_date(value: Any) -> date:
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        for pattern in ("%d/%m/%Y", "%d-%m-%Y", "%b %Y", "%B %Y"):
            try:
                parsed = datetime.strptime(text, pattern).date()
                return parsed.replace(day=1) if "%Y" in pattern and "%d" not in pattern else parsed
            except ValueError:
                continue
        raise ValueError(f"Unsupported date format: {text}")


def _nonnegative_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def compare_claims(
    claims: dict[str, Any], record: dict[str, Any] | None, retrieved_at: str
) -> list[dict[str, Any]]:
    if not record:
        return []
    if record.get("ambiguous") or record.get("source_error"):
        return []

    findings: list[dict[str, Any]] = []
    source_url = record.get("project_url") or "https://rera.karnataka.gov.in/"
    rera_id = record.get("rera_id")

    def add(kind: str, severity: str, title: str, points: int, excerpt: str) -> None:
        findings.append(
            {
                "type": kind,
                "severity": severity,
                "title": title,
                "points": points,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "tinyfish_tool": "run_web_automation",
                "run_id": record.get("run_id") or record.get("trace_id"),
                "excerpt": excerpt,
            }
        )

    listing_date = claims.get("possession_date")
    official_date = record.get("revised_completion_date")
    if listing_date and official_date:
        try:
            listing = _parse_date(listing_date)
            official = _parse_date(official_date)
            if official > _add_months(listing, 18):
                add(
                    "possession_mismatch",
                    "critical",
                    "Possession timeline differs by more than 18 months",
                    25,
                    f"Listing: {listing.isoformat()}; RERA revised completion: {official.isoformat()}.",
                )
            elif official > _add_months(listing, 6):
                add(
                    "possession_mismatch",
                    "warning",
                    "Possession timeline differs by more than 6 months",
                    15,
                    f"Listing: {listing.isoformat()}; RERA revised completion: {official.isoformat()}.",
                )
        except ValueError:
            pass

    claimed_id = normalized(claims.get("rera_id"))
    official_id = normalized(rera_id)
    if claimed_id and official_id and claimed_id != official_id:
        add(
            "rera_id_mismatch",
            "critical",
            "Listing RERA ID does not match the retrieved record",
            25,
            f"Listing ID: {claims.get('rera_id')}; RERA record ID: {rera_id}.",
        )
    elif not claimed_id and rera_id:
        add(
            "missing_listing_rera_id",
            "warning",
            "Listing does not show a RERA ID",
            5,
            f"The retrieved RERA record lists ID {rera_id}; no ID was provided in the listing claims.",
        )

    claimed_builder = normalized(claims.get("builder"))
    official_builder = normalized(record.get("promoter"))
    if claimed_builder and official_builder and claimed_builder not in official_builder:
        add(
            "builder_mismatch",
            "warning",
            "Builder name differs from the RERA promoter name",
            10,
            f"Listing builder: {claims.get('builder')}; RERA promoter: {record.get('promoter')}.",
        )

    claimed_project = normalized(claims.get("project_name"))
    official_project = normalized(record.get("project_name"))
    if claimed_project and official_project and claimed_project not in official_project:
        add(
            "project_name_mismatch",
            "warning",
            "Project name differs from the RERA record",
            10,
            f"Listing project: {claims.get('project_name')}; RERA project: {record.get('project_name')}.",
        )

    status = str(record.get("status") or "").lower()
    if not record.get("found") or any(
        word in status
        for word in ("lapsed", "revoked", "expired", "not registered", "inactive", "cancelled")
    ):
        add(
            "registration_status",
            "critical",
            "RERA registration was not found or is not active",
            40,
            f"RERA reported status: {record.get('status') or 'not found'}.",
        )
    return findings


def evidence_findings(
    news: list[dict[str, Any]],
    residents: list[dict[str, Any]],
    retrieved_at: str,
    locality: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    negative_news = next(
        (
            item
            for item in news
            if item.get("classification", {}).get("verified")
            and item["classification"].get("relevant") is True
            and item["classification"].get("negative") is True
        ),
        None,
    )
    if negative_news:
        classification = negative_news["classification"]
        findings.append(
            {
                "type": "negative_news",
                "severity": "warning",
                "title": "Recent negative news coverage was found",
                "points": 10,
                "source_url": negative_news.get("url"),
                "evidence_urls": [negative_news.get("url")],
                "retrieved_at": retrieved_at,
                "tinyfish_tool": negative_news.get("tinyfish_tool", "search"),
                "run_id": negative_news.get("trace_id") or negative_news.get("run_id"),
                "excerpt": classification["excerpt"],
            }
        )

    issue_categories = {
        "waterlogging": {"waterlogging", "flooding", "drainage"},
        "power": {"power"},
        "water_supply": {"water_supply"},
        "structural": {"structural"},
        "traffic": {"traffic"},
    }
    all_residents = residents + [
        item
        for item in (locality or [])
        if item.get("source_type") == "resident_forum"
    ]
    verified_categories: dict[str, list[dict[str, Any]]] = {}
    for category, equivalent_categories in issue_categories.items():
        matching = [
            item
            for item in all_residents
            if item.get("classification", {}).get("verified")
            and item["classification"].get("relevant") is True
            and item["classification"].get("issue_category") in equivalent_categories
            and item.get("url")
        ]
        independent_threads = list({item["url"]: item for item in matching}.values())
        if len(independent_threads) < 2:
            continue
        verified_categories[category] = independent_threads[:2]
    if verified_categories:
        evidence = [
            item
            for category_sources in verified_categories.values()
            for item in category_sources
        ]
        findings.append(
            {
                "type": "resident_issue",
                "severity": "warning",
                "title": (
                    "Multiple public resident threads report "
                    + ", ".join(
                        (
                            "waterlogging / flooding / drainage"
                            if category == "waterlogging"
                            else category.replace("_", " ")
                        )
                        for category in verified_categories
                    )
                    + " issues"
                ),
                "points": 10,
                "source_url": evidence[0]["url"],
                "evidence_urls": [item["url"] for item in evidence],
                "retrieved_at": retrieved_at,
                "tinyfish_tool": evidence[0].get("tinyfish_tool", "search"),
                "run_id": evidence[0].get("trace_id") or evidence[0].get("run_id"),
                "excerpt": " ".join(
                    item["classification"]["excerpt"] for item in evidence
                ),
            }
        )
    return findings


def calculate_score(
    record: dict[str, Any] | None,
    findings: list[dict[str, Any]],
    builder_projects: list[dict[str, Any]] | None = None,
    news_count: int = 0,
    resident_count: int = 0,
    failed_agents: int = 0,
) -> dict[str, Any]:
    deductions: list[dict[str, Any]] = []
    seen_types: set[str] = set()
    for finding in findings:
        kind = finding["type"]
        if kind in seen_types and kind not in {"complaint", "extension"}:
            continue
        seen_types.add(kind)
        if finding.get("points", 0) > 0:
            deductions.append(finding)

    if record:
        extensions = _nonnegative_count(record.get("extensions_count"))
        complaints = _nonnegative_count(record.get("complaints_count"))
        if extensions:
            deductions.append(
                {
                    "type": "deadline_extensions",
                    "severity": "warning",
                    "title": f"{extensions} deadline extension(s) on the RERA record",
                    "points": min(8 * extensions, 20),
                    "source_url": record.get("project_url") or "https://rera.karnataka.gov.in/",
                    "retrieved_at": record.get("retrieved_at"),
                    "tinyfish_tool": "run_web_automation",
                    "run_id": record.get("run_id") or record.get("trace_id"),
                    "excerpt": f"RERA record reports {extensions} extension(s).",
                }
            )
        if complaints:
            deductions.append(
                {
                    "type": "registered_complaints",
                    "severity": "warning",
                    "title": f"{complaints} registered complaint(s) reported",
                    "points": min(3 * complaints, 15),
                    "source_url": record.get("project_url") or "https://rera.karnataka.gov.in/",
                    "retrieved_at": record.get("retrieved_at"),
                    "tinyfish_tool": "run_web_automation",
                    "run_id": record.get("run_id") or record.get("trace_id"),
                    "excerpt": f"RERA record reports {complaints} complaint(s).",
                }
            )

    checked_projects = builder_projects or []
    if checked_projects:
        delayed_or_lapsed = [
            project
            for project in checked_projects
            if _nonnegative_count(project.get("extensions_count")) > 0
            or any(
                word in str(project.get("status") or "").lower()
                for word in ("lapsed", "revoked", "expired", "inactive")
            )
        ]
        points = min(
            15,
            round(15 * len(delayed_or_lapsed) / len(checked_projects)),
        )
        if points:
            source = delayed_or_lapsed[0]
            deductions.append(
                {
                    "type": "builder_track_record",
                    "severity": "warning",
                    "title": (
                        f"{len(delayed_or_lapsed)} of {len(checked_projects)} "
                        "checked builder projects had extensions or inactive status"
                    ),
                    "points": points,
                    "source_url": source.get("project_url")
                    or "https://rera.karnataka.gov.in/",
                    "evidence_urls": [
                        project.get("project_url")
                        for project in delayed_or_lapsed
                        if project.get("project_url")
                    ],
                    "retrieved_at": source.get("retrieved_at"),
                    "tinyfish_tool": "run_web_automation",
                    "run_id": source.get("run_id"),
                    "excerpt": (
                        "Builder portfolio checked against other Karnataka RERA "
                        "project records; each portfolio project is shown separately."
                    ),
                }
            )

    total = min(100, sum(item["points"] for item in deductions))
    score = max(0, 100 - total)
    if score >= 80:
        band = "Low"
    elif score >= 60:
        band = "Moderate"
    elif score >= 40:
        band = "High"
    else:
        band = "Critical"

    unknown = (
        not record
        or record.get("ambiguous")
        or record.get("source_error")
        or not (record.get("rera_id") or record.get("project_url"))
    )
    if unknown or not record.get("found"):
        confidence = "Low"
    elif failed_agents:
        confidence = "Medium"
    elif any(
        record.get(field) is None
        for field in (
            "status",
            "original_completion_date",
            "revised_completion_date",
            "extensions_count",
            "complaints_count",
        )
    ):
        confidence = "Medium"
    elif news_count + resident_count >= 2:
        confidence = "High"
    else:
        confidence = "Medium"

    return {
        "score": score if not unknown else None,
        "risk_band": band if not unknown else "Unknown",
        "confidence": confidence,
        "deductions": deductions,
        "sources_checked": news_count + resident_count,
    }
