"""Offline scoring heuristic tests."""

from __future__ import annotations

from jobhunt_engine import scoring

CFG = {
    "candidate": {
        "locations_ok": ["Delhi", "Gurgaon", "Noida"],
        "min_salary_lpa": 12,
        "preferred_titles": ["Backend Engineer", "Java Developer"],
    }
}


def test_strong_backend_job_scores_well():
    result = scoring.heuristic_score(
        {
            "title": "Senior Java Backend Engineer",
            "description": (
                "Build Spring Boot microservices and REST APIs with PostgreSQL, "
                "Kafka, Docker and AWS. Work on distributed systems and SQL."
            ),
            "location": "Delhi",
            "salary_text": "18-24 LPA",
            "is_remote": False,
        },
        CFG,
    )

    assert result["score"] >= 80
    assert result["salary_ok"] == "yes"
    assert result["score_method"] == "heuristic"
    assert result["red_flags"] == ""


def test_unrelated_frontend_job_scores_low():
    result = scoring.heuristic_score(
        {
            "title": "Frontend React Developer",
            "description": "Build React UI components for a consumer web app.",
            "location": "Mumbai",
            "is_remote": False,
        },
        CFG,
    )

    assert result["score"] < 30
    assert "frontend" in result["red_flags"]
    assert "location" in result["red_flags"]


def test_high_experience_requirement_is_penalised():
    base = {
        "title": "Java Backend Engineer",
        "description": "Spring Boot and REST APIs.",
        "location": "Delhi",
        "is_remote": False,
    }
    normal = scoring.heuristic_score(base, CFG)
    senior = scoring.heuristic_score(
        {**base, "description": base["description"] + " Requires 8+ years."},
        CFG,
    )

    assert senior["score"] < normal["score"]
    assert "8+ years" in senior["red_flags"]


def test_salary_parsing():
    assert scoring._salary_ok("18-24 LPA", 12) == "yes"
    assert scoring._salary_ok("8 LPA", 12) == "no"
    assert scoring._salary_ok("₹25,00,000 per year", 12) == "yes"
    assert scoring._salary_ok("", 12) == "unknown"


def test_years_required_parsing():
    assert scoring._years_required("Minimum 5 years of experience") == 5
    assert scoring._years_required("8+ years building backends") == 8
    assert scoring._years_required("No experience requirement") is None
