"""Resume tailoring validation tests."""

from __future__ import annotations

from jobhunt_engine import resume

MASTER = {
    "name": "Test Candidate",
    "headline": "Backend Engineer",
    "email": "test@example.com",
    "summary": "Backend engineer with 5 years of experience building Java services.",
    "skills": [
        {"category": "Languages", "items": ["Java", "SQL"]},
        {"category": "Frameworks", "items": ["Spring Boot", "Hibernate"]},
    ],
    "experience": [
        {
            "company": "Acme",
            "title": "Software Engineer",
            "location": "Remote",
            "start": "Jan 2020",
            "end": "Present",
            "bullets": [
                "Built REST APIs with Java and Spring Boot for a payments platform.",
                "Reduced p95 latency by 35% by adding PostgreSQL indexes.",
            ],
        },
        {
            "company": "Beta",
            "title": "Junior Engineer",
            "bullets": ["Wrote integration tests for a logistics dashboard."],
        },
    ],
    "projects": [
        {
            "name": "Order Service",
            "tech": "Java, Kafka",
            "bullets": ["Implemented idempotent Kafka consumers."],
        }
    ],
}


def test_unknown_skills_and_new_metrics_are_dropped():
    data = {
        "headline": "Java Backend Engineer",
        "summary": "Backend engineer with 5 years of experience building Java services.",
        "skills": [
            {"category": "Languages", "items": ["Java", "Kubernetes"]},
        ],
        "experience": [
            {
                "company": "Acme",
                "bullets": [
                    "Built REST APIs with Java and Spring Boot for a payments platform.",
                    "Improved throughput by 99%.",
                ],
            }
        ],
        "projects": [],
    }

    tailored, warnings = resume.validate_tailoring(MASTER, data)

    skill_items = [item for group in tailored["skills"] for item in group["items"]]
    assert "Java" in skill_items
    assert "Kubernetes" not in skill_items
    assert any("unknown skill" in warning for warning in warnings)

    acme_bullets = tailored["experience"][0]["bullets"]
    assert any("Built REST APIs" in bullet for bullet in acme_bullets)
    assert all("99%" not in bullet for bullet in acme_bullets)
    assert any("new metric" in warning for warning in warnings)


def test_every_master_role_is_kept_even_when_llm_omits_it():
    data = {
        "experience": [
            {
                "company": "Acme",
                "bullets": [
                    "Built REST APIs with Java and Spring Boot for a payments platform."
                ],
            }
        ]
    }

    tailored, _ = resume.validate_tailoring(MASTER, data)

    companies = [item["company"] for item in tailored["experience"]]
    assert companies == ["Acme", "Beta"]
    assert tailored["experience"][1]["bullets"] == MASTER["experience"][1]["bullets"]


def test_summary_with_new_number_is_replaced():
    data = {"summary": "Backend engineer with 12 years of experience."}

    tailored, warnings = resume.validate_tailoring(MASTER, data)

    assert tailored["summary"] == MASTER["summary"]
    assert any("new number" in warning for warning in warnings)


def test_projects_fall_back_to_master_when_none_are_tailored():
    tailored, _ = resume.validate_tailoring(MASTER, {"experience": []})

    assert tailored["projects"] == MASTER["projects"]


def test_all_master_projects_are_kept_even_when_llm_omits_one():
    master = dict(MASTER)
    master["projects"] = MASTER["projects"] + [
        {
            "name": "Second Project",
            "tech": "Python",
            "bullets": ["Built a Python service."],
        }
    ]

    tailored, _ = resume.validate_tailoring(
        master,
        {
            "projects": [
                {
                    "name": "Order Service",
                    "bullets": ["Implemented idempotent Kafka consumers."],
                }
            ]
        },
    )

    assert [project["name"] for project in tailored["projects"]] == [
        "Order Service",
        "Second Project",
    ]


def test_invalid_tailoring_payload_uses_offline_selection():
    tailored, warnings = resume.validate_tailoring(MASTER, "not a dict")

    assert warnings == ["LLM returned invalid tailoring JSON"]
    assert tailored["headline"] == MASTER["headline"]
    assert tailored["summary"] == MASTER["summary"].strip()


def test_offline_tailor_only_uses_master_facts():
    tailored = resume._offline_tailor(
        {
            "title": "Java Backend Engineer",
            "description": "Java Spring Boot PostgreSQL Kafka microservices",
        },
        MASTER,
        {"candidate": {"preferred_titles": ["Backend Engineer"]}},
    )

    known_skills = {item for group in MASTER["skills"] for item in group["items"]}
    tailored_skills = {item for group in tailored["skills"] for item in group["items"]}
    assert tailored_skills <= known_skills
    assert tailored["experience"][0]["title"] == "Software Engineer"
    assert tailored["experience"][0]["location"] == "Remote"
    assert tailored["experience"][0]["start"] == "Jan 2020"
    assert tailored["experience"][0]["end"] == "Present"
    assert tailored["projects"][0]["tech"] == "Java, Kafka"
    assert all(
        bullet in MASTER["experience"][0]["bullets"]
        for bullet in tailored["experience"][0]["bullets"]
    )
    assert "java" in tailored["keywords"]
