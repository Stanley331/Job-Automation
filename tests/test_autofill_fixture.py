"""End-to-end autofill test against the local fixture form.

The browser fills fields but must never submit the form. This test is skipped
automatically when Playwright or its Chromium build is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")

from jobhunt_engine import db
from jobhunt_engine.autofill import run_autofill
from jobhunt_engine.config import ensure_dirs, load_config

FIXTURE = Path(__file__).with_name("fixture_form.html")


def test_fixture_form_is_filled_without_submitting(tmp_path):
    cfg = load_config()
    cfg["_root"] = str(tmp_path)
    cfg["database"] = str(tmp_path / "jobs.db")
    cfg["output"]["dir"] = str(tmp_path / "output")
    cfg["output"]["report_path"] = str(tmp_path / "output" / "report.csv")
    cfg["autofill"].update(
        {
            "browser_profile": str(tmp_path / "profile"),
            "headless": True,
            "click_apply": False,
            "slow_mo_ms": 0,
        }
    )
    ensure_dirs(cfg)

    resume_file = tmp_path / "output" / "resumes" / "test.pdf"
    resume_file.write_bytes(b"%PDF-1.4\n% fixture resume\n%%EOF\n")

    conn = db.connect(cfg)
    try:
        job_id, _ = db.upsert_job(
            conn,
            {
                "url": FIXTURE.resolve().as_uri(),
                "title": "Senior Java Backend Engineer",
                "company": "Acme Systems",
                "description": "Java Spring Boot backend role.",
            },
        )
        db.upsert_application(
            conn,
            job_id,
            resume_pdf="resumes/test.pdf",
            answers_json=[
                {
                    "question": "Why are you interested in this role at Acme Systems?",
                    "answer": "I want to build reliable backend services with this team.",
                    "source": "user",
                },
                {
                    "question": "Tell us about a challenging project you worked on.",
                    "answer": "I migrated a monolith to two microservices and cut deploy time.",
                    "source": "user",
                },
            ],
        )
    finally:
        conn.close()

    try:
        summary = run_autofill(
            job_id,
            cfg=cfg,
            click_apply=False,
            headless=True,
            keep_open=False,
            progress=lambda _message: None,
        )
    except Exception as exc:  # pragma: no cover - environment dependent
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Playwright Chromium is not installed")
        raise

    assert summary["fields"] > 0
    assert summary["filled"] >= 10
    assert any("test.pdf" in item for item in summary["uploaded"])
    assert summary["url"].startswith("file://")

    conn = db.connect(cfg)
    try:
        event = conn.execute(
            "SELECT * FROM events WHERE job_id = ? AND kind = 'autofill'", (job_id,)
        ).fetchone()
        assert event is not None
    finally:
        conn.close()
