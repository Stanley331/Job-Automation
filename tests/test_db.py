"""Database schema, migration and application tracking tests."""

from __future__ import annotations

import sqlite3

import pytest

from jobhunt_engine import db


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    db.init_db(connection)
    yield connection
    connection.close()


def _job(**overrides) -> dict:
    data = {
        "url": "https://example.com/jobs/1",
        "title": "Java Backend Engineer",
        "company": "Acme Systems",
        "location": "Delhi",
        "site": "example",
        "is_remote": False,
        "description": "Short description.",
        "salary_text": "18 LPA",
    }
    data.update(overrides)
    return data


def test_upsert_job_deduplicates_and_keeps_longer_description(conn):
    job_id, inserted = db.upsert_job(conn, _job())
    assert inserted is True
    assert job_id > 0

    same_id, inserted_again = db.upsert_job(
        conn,
        _job(
            title="Different title should not overwrite",
            description="A much longer and more useful description of the role.",
            apply_url="https://example.com/jobs/1/apply",
        ),
    )

    assert inserted_again is False
    assert same_id == job_id
    row = db.get_job(conn, job_id)
    assert row["title"] == "Java Backend Engineer"
    assert row["apply_url"] == "https://example.com/jobs/1/apply"
    assert (
        row["description"] == "A much longer and more useful description of the role."
    )


def test_status_applied_sets_applied_at_and_logs_event(conn):
    job_id, _ = db.upsert_job(conn, _job())

    db.set_status(conn, job_id, "applied", note="submitted manually")

    assert db.get_job(conn, job_id)["status"] == "applied"
    application = db.get_application(conn, job_id)
    assert application["applied_at"]

    event = conn.execute(
        "SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()
    assert event["kind"] == "status"
    assert "submitted manually" in event["detail"]


def test_invalid_status_is_rejected(conn):
    job_id, _ = db.upsert_job(conn, _job())
    with pytest.raises(ValueError):
        db.set_status(conn, job_id, "submitted")


def test_legacy_schema_is_migrated_without_losing_jobs():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            company TEXT,
            location TEXT,
            site TEXT,
            is_remote INTEGER DEFAULT 0,
            date_posted TEXT,
            salary_text TEXT,
            description TEXT,
            score INTEGER,
            reason TEXT,
            red_flags TEXT,
            salary_ok TEXT,
            status TEXT DEFAULT 'new',
            first_seen TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO jobs (
            url, title, company, location, site, is_remote, description,
            score, reason, status, first_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "https://legacy.example/job",
            "Legacy Java Role",
            "Old Co",
            "Noida",
            "naukri",
            1,
            "Legacy description",
            77,
            "good fit",
            "shortlisted",
            "2026-01-01T00:00:00+00:00",
        ),
    )

    db.init_db(connection)

    row = connection.execute("SELECT * FROM jobs").fetchone()
    assert row["url"] == "https://legacy.example/job"
    assert row["apply_url"] == "https://legacy.example/job"
    assert row["title"] == "Legacy Java Role"
    assert row["score"] == 77
    assert row["status"] == "shortlisted"
    assert row["score_method"] == "legacy"
    assert row["last_seen"] == row["first_seen"]
    assert (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs_legacy'"
        ).fetchone()
        is None
    )
    connection.close()


def test_resolve_job_by_id_url_and_title(conn):
    first_id, _ = db.upsert_job(conn, _job())
    second_id, _ = db.upsert_job(
        conn,
        _job(
            url="https://example.com/jobs/2",
            title="Frontend React Developer",
            company="Other Co",
        ),
    )

    assert db.resolve_job(conn, str(first_id))["id"] == first_id
    assert db.resolve_job(conn, "https://example.com/jobs/2")["id"] == second_id
    assert db.resolve_job(conn, "React Developer")["id"] == second_id
    assert db.resolve_job(conn, "does-not-exist") is None
