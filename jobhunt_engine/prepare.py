"""Orchestrates one application: tailor resume, cover letter and answers."""

from __future__ import annotations

import json
from collections.abc import Callable

from . import answers as answers_mod
from . import db
from . import resume as resume_mod
from .config import (
    ConfigError,
    candidate_profile_text,
    ensure_dirs,
    flat_candidate,
    load_answer_bank,
    load_config,
    load_resume,
    placeholder_issues,
)
from .llm import LLM


def prepare_job(
    job_ref: str | int,
    cfg: dict | None = None,
    force: bool = False,
    cover_letter: bool | None = None,
    progress: Callable[[str], None] = print,
) -> dict:
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    master = load_resume()
    issues = placeholder_issues(master)
    if issues and not force:
        raise ConfigError(
            "Your master resume is still a template:\n  - "
            + "\n  - ".join(issues)
            + "\nEdit data/master_resume.yaml (or run `python jobhunt.py import-resume <file>`) "
            "and try again. Use --force only for a dry run."
        )

    conn = db.connect(cfg)
    try:
        job = db.resolve_job(conn, job_ref)
        if not job:
            raise ConfigError(
                f"No job found matching {job_ref!r}. Try `python jobhunt.py list`."
            )
        job_id = int(job["id"])
        progress(
            f"Preparing application for: {job.get('title')} @ {job.get('company')}"
        )

        llm = LLM(cfg)
        progress(f"Using {llm.describe()}")
        tailored, warnings = resume_mod.tailor_resume(job, master, cfg, llm)

        progress("Rendering tailored resume (HTML, DOCX, PDF)...")
        resume_artifacts = resume_mod.render_resume(job, master, tailored, cfg)
        warnings += resume_artifacts.get("warnings", [])

        cover_artifacts: dict = {}
        cover_text = ""
        cover_method = ""
        want_cover = (
            cover_letter
            if cover_letter is not None
            else cfg["output"].get("generate_cover_letter", True)
        )
        if want_cover:
            progress("Writing cover letter...")
            cover_text, cover_warnings, cover_method = resume_mod.write_cover_letter(
                job, master, tailored, cfg, llm
            )
            warnings += cover_warnings
            cover_artifacts = resume_mod.render_cover_letter(
                job, master, cover_text, cfg
            )
            warnings += cover_artifacts.get("warnings", [])

        progress("Drafting screening answers...")
        candidate = flat_candidate(cfg, master)
        bank = load_answer_bank()
        drafted = answers_mod.draft_answers(job, master, cfg, llm, bank, candidate)

        meta = {
            "llm": llm.describe(),
            "tailored": tailored,
            "warnings": warnings,
            "cover_letter_text": cover_text,
            "cover_letter_method": cover_method,
            "candidate_profile": candidate_profile_text(cfg, master),
        }
        db.upsert_application(
            conn,
            job_id,
            resume_pdf=resume_artifacts.get("pdf"),
            resume_docx=resume_artifacts.get("docx"),
            resume_html=resume_artifacts.get("html"),
            cover_letter_pdf=cover_artifacts.get("pdf"),
            cover_letter_html=cover_artifacts.get("html"),
            answers_json=drafted,
            meta_json=meta,
        )
        if job.get("status") in ("new", "shortlisted"):
            db.set_status(
                conn, job_id, "prepared", note="tailored application generated"
            )
        db.log_event(
            conn,
            job_id,
            "prepare",
            json.dumps(
                {
                    "resume": resume_artifacts.get("pdf")
                    or resume_artifacts.get("docx"),
                    "answers": len(drafted),
                    "warnings": len(warnings),
                }
            ),
        )

        progress("")
        progress("Application package ready:")
        progress(
            f"  Resume:       {resume_artifacts.get('pdf') or resume_artifacts.get('docx')}"
        )
        if cover_artifacts:
            progress(
                f"  Cover letter: {cover_artifacts.get('pdf') or cover_artifacts.get('html')}"
            )
        progress(f"  Answers:      {len(drafted)} drafted")
        if warnings:
            progress("")
            progress("Review notes:")
            for warning in warnings:
                progress(f"  ! {warning}")
        progress("")
        progress(
            "Next: `python jobhunt.py apply " + str(job_id) + "` or use the dashboard."
        )

        return {
            "job_id": job_id,
            "title": job.get("title"),
            "company": job.get("company"),
            "resume": resume_artifacts,
            "cover_letter": cover_artifacts,
            "answers": drafted,
            "warnings": warnings,
            "llm": llm.describe(),
        }
    finally:
        conn.close()
