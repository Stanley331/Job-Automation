"""Local FastAPI dashboard for reviewing and tracking applications."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .config import (
    ROOT,
    ConfigError,
    ensure_dirs,
    load_config,
    load_resume,
    output_dir,
    placeholder_issues,
)
from .llm import llm_status

TEMPLATES = Path(__file__).resolve().parent / "templates"


def _get_job_or_404(conn, job_id: int) -> dict:
    job = db.get_job(conn, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def create_app(cfg: dict | None = None) -> FastAPI:
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    app = FastAPI(title="JobHunt", version="2.0.0")

    out = output_dir(cfg)
    app.mount("/files", StaticFiles(directory=str(out)), name="files")

    def with_conn():
        return db.connect(cfg)

    @app.get("/", response_class=HTMLResponse)
    def index():
        path = TEMPLATES / "dashboard.html"
        if not path.exists():
            return HTMLResponse("<h1>Dashboard template missing</h1>", status_code=500)
        return HTMLResponse(path.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def health():
        master = load_resume()
        issues = placeholder_issues(master)
        return {
            "ok": True,
            "llm": llm_status(cfg),
            "resume_ready": not issues,
            "resume_issues": issues,
            "database": str(cfg["database"]),
            "output": str(cfg["output"]["dir"]),
        }

    @app.get("/api/stats")
    def api_stats():
        conn = with_conn()
        try:
            return db.stats(conn)
        finally:
            conn.close()

    @app.get("/api/jobs")
    def api_jobs(
        status: str = Query(default=""),
        q: str = Query(default=""),
        min_score: int = Query(default=0),
        limit: int = Query(default=250, ge=1, le=1000),
    ):
        conn = with_conn()
        try:
            jobs = db.list_jobs(
                conn,
                status=status or None,
                q=q or None,
                min_score=min_score,
                limit=limit,
            )
            for job in jobs:
                job["has_resume"] = bool(
                    job.get("resume_pdf") or job.get("resume_docx")
                )
            return {"jobs": jobs}
        finally:
            conn.close()

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int):
        conn = with_conn()
        try:
            job = db.job_detail(conn, job_id)
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            return job
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/status")
    def api_status(job_id: int, payload: Annotated[dict, Body()]):
        status = str(payload.get("status") or "").strip()
        note = payload.get("note")
        if status not in db.STATUSES:
            raise HTTPException(status_code=400, detail=f"Unknown status {status!r}")
        conn = with_conn()
        try:
            _get_job_or_404(conn, job_id)
            db.set_status(conn, job_id, status, note=str(note) if note else None)
            return {"ok": True, "job": db.get_job(conn, job_id)}
        finally:
            conn.close()

    @app.put("/api/jobs/{job_id}/answers")
    def api_answers(job_id: int, payload: Annotated[dict, Body()]):
        answers = payload.get("answers")
        if not isinstance(answers, list):
            raise HTTPException(status_code=400, detail="answers must be a list")
        cleaned = []
        for item in answers:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            cleaned.append(
                {
                    "question": question,
                    "answer": str(item.get("answer") or "").strip(),
                    "source": str(item.get("source") or "user"),
                    "needs_review": bool(item.get("needs_review")),
                }
            )
        conn = with_conn()
        try:
            _get_job_or_404(conn, job_id)
            db.save_answers(conn, job_id, cleaned)
            db.log_event(conn, job_id, "answers_updated", f"{len(cleaned)} answers")
            return {"ok": True, "answers": cleaned}
        finally:
            conn.close()

    @app.put("/api/jobs/{job_id}/notes")
    def api_notes(job_id: int, payload: Annotated[dict, Body()]):
        notes = str(payload.get("notes") or "")
        conn = with_conn()
        try:
            _get_job_or_404(conn, job_id)
            db.upsert_application(conn, job_id, notes=notes)
            db.log_event(conn, job_id, "notes", notes[:300])
            return {"ok": True}
        finally:
            conn.close()

    @app.post("/api/jobs/{job_id}/prepare")
    def api_prepare(job_id: int, payload: Annotated[dict | None, Body()] = None):
        from .prepare import prepare_job

        force = bool((payload or {}).get("force", False))
        logs: list[str] = []
        conn = with_conn()
        try:
            _get_job_or_404(conn, job_id)
        finally:
            conn.close()
        try:
            result = prepare_job(job_id, cfg=cfg, force=force, progress=logs.append)
            return {"ok": True, "result": result, "logs": logs}
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"{type(exc).__name__}: {exc}"
            ) from exc

    @app.post("/api/jobs/{job_id}/autofill")
    def api_autofill(job_id: int):
        conn = with_conn()
        try:
            _get_job_or_404(conn, job_id)
            app = db.get_application(conn, job_id)
            if not app or not (app.get("resume_pdf") or app.get("resume_docx")):
                raise HTTPException(
                    status_code=400,
                    detail="Prepare the application first so there is a tailored resume to upload.",
                )
        finally:
            conn.close()

        cmd = [sys.executable, "-m", "jobhunt_engine.cli", "apply", str(job_id)]
        kwargs: dict = {"cwd": str(ROOT)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not launch browser: {exc}"
            ) from exc
        return {
            "ok": True,
            "pid": proc.pid,
            "message": "A browser window is opening. Review the form, then submit it yourself.",
        }

    @app.post("/api/jobs/{job_id}/open")
    def api_open(job_id: int):
        conn = with_conn()
        try:
            job = _get_job_or_404(conn, job_id)
            url = job.get("apply_url") or job.get("url")
            if not url:
                raise HTTPException(status_code=400, detail="Job has no URL")
            webbrowser.open(url)
            return {"ok": True, "url": url}
        finally:
            conn.close()

    run_state: dict = {"proc": None}

    @app.post("/api/run")
    def api_run(payload: Annotated[dict | None, Body()] = None):
        """Start a discovery + scoring run in its own console window."""
        proc = run_state.get("proc")
        if proc is not None and proc.poll() is None:
            raise HTTPException(
                status_code=409,
                detail="A search is already running. Check the console window it opened.",
            )

        payload = payload or {}
        cmd = [sys.executable, "-m", "jobhunt_engine.cli", "run"]
        limit = payload.get("limit")
        if limit:
            try:
                cmd += ["--limit", str(int(limit))]
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="limit must be a number")
        prepare_top = payload.get("prepare_top")
        if prepare_top:
            try:
                cmd += ["--prepare-top", str(int(prepare_top))]
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail="prepare_top must be a number"
                )
        if payload.get("rescore"):
            cmd.append("--rescore")

        kwargs: dict = {"cwd": str(ROOT)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"Could not start the search: {exc}"
            ) from exc

        run_state["proc"] = proc
        return {
            "ok": True,
            "pid": proc.pid,
            "message": (
                "Search started in a new console window. It searches the boards, "
                "scores new jobs and writes the report. This page refreshes when it finishes."
            ),
        }

    @app.get("/api/run/status")
    def api_run_status():
        proc = run_state.get("proc")
        if proc is None:
            return {"running": False, "started": False}
        code = proc.poll()
        return {
            "running": code is None,
            "started": True,
            "exit_code": code,
        }

    @app.get("/api/answer-bank")
    def api_answer_bank():
        from .config import load_answer_bank

        return {"answers": load_answer_bank()}

    @app.get("/api/events")
    def api_events(limit: int = Query(default=50, ge=1, le=500)):
        conn = with_conn()
        try:
            rows = conn.execute(
                """SELECT e.*, j.title, j.company FROM events e
                   LEFT JOIN jobs j ON j.id = e.job_id
                   ORDER BY e.id DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return {"events": [dict(r) for r in rows]}
        finally:
            conn.close()

    return app


def serve_dashboard(
    cfg: dict | None = None,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool = True,
) -> None:
    import uvicorn

    cfg = cfg or load_config()
    host = host or cfg["dashboard"].get("host", "127.0.0.1")
    port = int(port or cfg["dashboard"].get("port", 8765))
    app = create_app(cfg)
    url = f"http://{host}:{port}"
    print(f"JobHunt dashboard: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    serve_dashboard()
