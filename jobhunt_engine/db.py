"""SQLite persistence: jobs, applications, events and status tracking."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import ROOT, db_path

STATUSES = [
    "new",
    "shortlisted",
    "prepared",
    "applied",
    "interview",
    "offer",
    "rejected",
    "ignored",
]
ACTIVE_STATUSES = ("new", "shortlisted", "prepared")
APPLIED_STATUSES = ("applied", "interview", "offer")

JOB_COLUMNS = [
    "url",
    "apply_url",
    "title",
    "company",
    "location",
    "site",
    "is_remote",
    "date_posted",
    "salary_text",
    "description",
    "score",
    "reason",
    "red_flags",
    "salary_ok",
    "score_method",
    "status",
    "search_term",
    "first_seen",
    "last_seen",
    "updated_at",
]

APPLICATION_FIELDS = {
    "resume_pdf",
    "resume_docx",
    "resume_html",
    "cover_letter_pdf",
    "cover_letter_html",
    "answers_json",
    "meta_json",
    "notes",
    "applied_at",
}


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _create_jobs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            apply_url TEXT,
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
            score_method TEXT,
            status TEXT DEFAULT 'new',
            search_term TEXT,
            first_seen TEXT,
            last_seen TEXT,
            updated_at TEXT
        )
        """
    )


def _migrate_jobs(conn: sqlite3.Connection) -> None:
    """Upgrade the original single-table schema without losing tracked jobs."""
    if not _table_exists(conn, "jobs"):
        _create_jobs_table(conn)
        return

    cols = _columns(conn, "jobs")
    needed = {
        "id",
        "apply_url",
        "score_method",
        "search_term",
        "last_seen",
        "updated_at",
    }
    if needed.issubset(set(cols)):
        return

    # Keep the old data. Foreign keys are enabled, so do this outside a
    # transaction that references the old table.
    conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
    _create_jobs_table(conn)
    old = set(cols)
    now = utcnow()

    def expr(column: str, fallback: str = "NULL") -> str:
        return f"COALESCE({column}, {fallback})" if column in old else fallback

    select_parts = [
        expr("url"),
        expr("url"),  # apply_url: original schema only had one URL
        expr("title"),
        expr("company"),
        expr("location"),
        expr("site"),
        expr("is_remote", "0"),
        expr("date_posted"),
        expr("salary_text"),
        expr("description"),
        expr("score"),
        expr("reason"),
        expr("red_flags"),
        expr("salary_ok"),
        "'legacy'",
        expr("status", "'new'"),
        "NULL",
        expr("first_seen", f"'{now}'"),
        expr("first_seen", f"'{now}'"),
        expr("first_seen", f"'{now}'"),
    ]
    conn.execute(
        f"INSERT INTO jobs ({', '.join(JOB_COLUMNS)}) SELECT {', '.join(select_parts)} FROM jobs_legacy"
    )
    conn.execute("DROP TABLE jobs_legacy")
    conn.commit()


def _create_support_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
            resume_pdf TEXT,
            resume_docx TEXT,
            resume_html TEXT,
            cover_letter_pdf TEXT,
            cover_letter_html TEXT,
            answers_json TEXT,
            meta_json TEXT,
            notes TEXT,
            applied_at TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id)")


def init_db(conn: sqlite3.Connection) -> None:
    _migrate_jobs(conn)
    _create_support_tables(conn)
    conn.commit()


def connect(cfg: dict | None = None) -> sqlite3.Connection:
    from .config import load_config

    cfg = cfg or load_config()
    path = db_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)

    # One-time import of the original root-level jobs.db.
    legacy = ROOT / "jobs.db"
    if not path.exists() and legacy.exists() and legacy.resolve() != path.resolve():
        try:
            shutil.copy2(legacy, path)
        except OSError:
            pass

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def upsert_job(conn: sqlite3.Connection, job: dict) -> tuple[int, bool]:
    """Insert a discovered job, or refresh last_seen. Returns (id, inserted)."""
    url = (job.get("url") or "").strip()
    if not url:
        raise ValueError("job.url is required")
    now = utcnow()
    existing = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE jobs SET
                last_seen = ?,
                apply_url = COALESCE(apply_url, ?),
                salary_text = COALESCE(salary_text, ?),
                description = CASE
                    WHEN ? IS NOT NULL
                         AND (description IS NULL OR length(description) < length(?))
                    THEN ? ELSE description END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                now,
                job.get("apply_url"),
                job.get("salary_text"),
                job.get("description"),
                job.get("description"),
                job.get("description"),
                now,
                existing["id"],
            ),
        )
        conn.commit()
        return int(existing["id"]), False

    cur = conn.execute(
        """
        INSERT INTO jobs (
            url, apply_url, title, company, location, site, is_remote,
            date_posted, salary_text, description, search_term,
            first_seen, last_seen, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            url,
            job.get("apply_url"),
            job.get("title"),
            job.get("company"),
            job.get("location"),
            job.get("site"),
            int(bool(job.get("is_remote"))),
            job.get("date_posted"),
            job.get("salary_text"),
            job.get("description"),
            job.get("search_term"),
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid), True


def resolve_job(conn: sqlite3.Connection, ref: str | int) -> dict | None:
    """Resolve an id, an exact URL, a URL fragment, or a title fragment."""
    s = str(ref).strip()
    row = None
    if s.isdigit():
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(s),)).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (s,)).fetchone()
    if row is None and s.startswith(("http://", "https://")):
        row = conn.execute(
            "SELECT * FROM jobs WHERE url LIKE ? ORDER BY score DESC LIMIT 1",
            (f"%{s}%",),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """SELECT * FROM jobs
               WHERE title LIKE ? OR company LIKE ?
               ORDER BY score IS NULL, score DESC, id DESC LIMIT 1""",
            (f"%{s}%", f"%{s}%"),
        ).fetchone()
    return row_to_dict(row)


def list_jobs(
    conn: sqlite3.Connection,
    status: str | None = None,
    q: str | None = None,
    min_score: int = 0,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if status:
        if status == "active":
            where.append("j.status IN ('new','shortlisted','prepared')")
        elif status in STATUSES:
            where.append("j.status = ?")
            params.append(status)
    if q:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if min_score:
        where.append("j.score >= ?")
        params.append(int(min_score))

    sql = """
        SELECT j.*,
               a.resume_pdf, a.resume_docx, a.resume_html,
               a.cover_letter_pdf, a.cover_letter_html,
               a.applied_at, a.notes AS application_notes
        FROM jobs j
        LEFT JOIN applications a ON a.job_id = j.id
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY j.score IS NULL, j.score DESC, j.id DESC LIMIT ? OFFSET ?"
    params += [int(limit), int(offset)]
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_job(conn: sqlite3.Connection, job_id: int) -> dict | None:
    return row_to_dict(
        conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
    )


def get_application(conn: sqlite3.Connection, job_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM applications WHERE job_id = ?", (int(job_id),)
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["answers"] = json.loads(data.get("answers_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        data["answers"] = []
    try:
        data["meta"] = json.loads(data.get("meta_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        data["meta"] = {}
    return data


def job_detail(conn: sqlite3.Connection, job_id: int) -> dict | None:
    job = get_job(conn, job_id)
    if not job:
        return None
    job["application"] = get_application(conn, job_id)
    job["events"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT 30",
            (int(job_id),),
        ).fetchall()
    ]
    return job


def upsert_application(conn: sqlite3.Connection, job_id: int, **fields: Any) -> dict:
    clean = {k: v for k, v in fields.items() if k in APPLICATION_FIELDS}
    if "answers_json" in clean and not isinstance(clean["answers_json"], str):
        clean["answers_json"] = json.dumps(clean["answers_json"], ensure_ascii=False)
    if "meta_json" in clean and not isinstance(clean["meta_json"], str):
        clean["meta_json"] = json.dumps(clean["meta_json"], ensure_ascii=False)

    now = utcnow()
    existing = conn.execute(
        "SELECT id FROM applications WHERE job_id = ?", (int(job_id),)
    ).fetchone()
    if existing:
        if clean:
            assignments = ", ".join(f"{k} = ?" for k in clean)
            conn.execute(
                f"UPDATE applications SET {assignments}, updated_at = ? WHERE job_id = ?",
                (*clean.values(), now, int(job_id)),
            )
    else:
        cols = ["job_id", *clean.keys(), "created_at", "updated_at"]
        vals = [int(job_id), *clean.values(), now, now]
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO applications ({', '.join(cols)}) VALUES ({placeholders})",
            vals,
        )
    conn.commit()
    return get_application(conn, job_id) or {}


def save_answers(
    conn: sqlite3.Connection, job_id: int, answers: Iterable[dict]
) -> None:
    upsert_application(conn, job_id, answers_json=list(answers))


def set_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    note: str | None = None,
) -> dict | None:
    if status not in STATUSES:
        raise ValueError(
            f"Unknown status {status!r}. Use one of: {', '.join(STATUSES)}"
        )
    now = utcnow()
    conn.execute(
        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
        (status, now, int(job_id)),
    )
    if status == "applied":
        app = get_application(conn, int(job_id))
        if not app or not app.get("applied_at"):
            upsert_application(conn, int(job_id), applied_at=now)
    detail = status if not note else f"{status}: {note}"
    log_event(conn, int(job_id), "status", detail)
    conn.commit()
    return get_job(conn, int(job_id))


def log_event(
    conn: sqlite3.Connection, job_id: int | None, kind: str, detail: str = ""
) -> None:
    conn.execute(
        "INSERT INTO events (job_id, kind, detail, created_at) VALUES (?,?,?,?)",
        (job_id, kind, detail, utcnow()),
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE score IS NOT NULL"
    ).fetchone()[0]
    avg = conn.execute(
        "SELECT AVG(score) FROM jobs WHERE score IS NOT NULL"
    ).fetchone()[0]
    by_status = {
        row["status"] or "new": row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
        ).fetchall()
    }
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
    applied_week = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE applied_at >= ?", (cutoff,)
    ).fetchone()[0]
    prepared = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE resume_pdf IS NOT NULL OR resume_docx IS NOT NULL"
    ).fetchone()[0]
    return {
        "total": total,
        "scored": scored,
        "avg_score": round(avg, 1) if avg is not None else None,
        "by_status": by_status,
        "applied_this_week": applied_week,
        "prepared": prepared,
    }


def report_rows(
    conn: sqlite3.Connection, min_score: int = 0, include_all: bool = False
) -> list[dict]:
    where = "WHERE score IS NOT NULL"
    params: list[Any] = []
    if not include_all:
        where += " AND score >= ?"
        params.append(int(min_score))
    sql = f"""
        SELECT id, score, status, title, company, location, is_remote,
               salary_text, salary_ok, reason, red_flags, date_posted, site,
               url, apply_url, first_seen, updated_at
        FROM jobs {where}
        ORDER BY score DESC, id DESC
    """
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def pending_scoring(
    conn: sqlite3.Connection, limit: int, rescore: bool = False
) -> list[dict]:
    where = "" if rescore else "WHERE score IS NULL"
    return [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY id ASC LIMIT ?", (int(limit),)
        ).fetchall()
    ]
