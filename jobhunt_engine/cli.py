"""Command-line interface for the JobHunt pipeline."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import sys
import textwrap
from pathlib import Path

from . import db
from .config import (
    ConfigError,
    ensure_dirs,
    load_answer_bank,
    load_config,
    load_resume,
    output_dir,
    placeholder_issues,
    report_path,
)
from .llm import llm_status


def _ok(message: str) -> None:
    print(f"  [ok]   {message}")


def _warn(message: str) -> None:
    print(f"  [warn] {message}")


def _fail(message: str) -> None:
    print(f"  [fail] {message}")


def _short(value, width: int) -> str:
    text = str(value or "")
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def _print_table(jobs: list[dict]) -> None:
    if not jobs:
        print(
            "No jobs found. Run `python jobhunt.py run` first, or loosen the filters."
        )
        return
    print(
        f"{'ID':>5}  {'SCORE':>5}  {'STATUS':<11}  {'TITLE':<42}  {'COMPANY':<24}  LOCATION"
    )
    print("-" * 118)
    for job in jobs:
        print(
            f"{job['id']:>5}  "
            f"{(job.get('score') if job.get('score') is not None else '-'):>5}  "
            f"{_short(job.get('status'), 11):<11}  "
            f"{_short(job.get('title'), 42):<42}  "
            f"{_short(job.get('company'), 24):<24}  "
            f"{_short(job.get('location'), 26)}"
        )
    print(f"\n{len(jobs)} job(s).")


def _write_report(cfg: dict, include_all: bool = False) -> Path:
    conn = db.connect(cfg)
    try:
        rows = db.report_rows(
            conn,
            min_score=int(cfg["scoring"].get("min_score_for_report", 60)),
            include_all=include_all,
        )
    finally:
        conn.close()
    dest = report_path(cfg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        list(rows[0].keys())
        if rows
        else [
            "id",
            "score",
            "status",
            "title",
            "company",
            "location",
            "is_remote",
            "salary_text",
            "salary_ok",
            "reason",
            "red_flags",
            "date_posted",
            "site",
            "url",
            "apply_url",
            "first_seen",
            "updated_at",
        ]
    )
    with dest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} job(s) to {dest}")
    return dest


def cmd_run(args: argparse.Namespace) -> int:
    from . import discovery, scoring
    from .prepare import prepare_job

    cfg = load_config()
    ensure_dirs(cfg)
    conn = db.connect(cfg)
    try:
        if not args.no_discover:
            discovery.discover(conn, cfg)
        if not args.no_score:
            scoring.score_jobs(
                conn,
                cfg,
                load_resume(),
                limit=args.limit,
                rescore=args.rescore,
            )
        if not args.no_report:
            _write_report(cfg)
        if args.prepare_top:
            rows = conn.execute(
                """SELECT id FROM jobs
                   WHERE status IN ('new','shortlisted') AND score IS NOT NULL
                     AND score >= ?
                     AND id NOT IN (
                        SELECT job_id FROM applications
                        WHERE resume_pdf IS NOT NULL OR resume_docx IS NOT NULL
                     )
                   ORDER BY score DESC, id DESC LIMIT ?""",
                (
                    int(cfg["scoring"].get("min_score_for_report", 60)),
                    int(args.prepare_top),
                ),
            ).fetchall()
            for row in rows:
                try:
                    prepare_job(row["id"], cfg=cfg, progress=print)
                except Exception as exc:  # noqa: BLE001 - keep preparing the rest
                    print(f"[warn] could not prepare job {row['id']}: {exc}")
    finally:
        conn.close()
    print()
    print("Next: `python jobhunt.py dashboard` to review, prepare and autofill.")
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    from . import discovery

    cfg = load_config()
    conn = db.connect(cfg)
    try:
        discovery.discover(conn, cfg)
    finally:
        conn.close()
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from . import scoring

    cfg = load_config()
    conn = db.connect(cfg)
    try:
        scoring.score_jobs(
            conn, cfg, load_resume(), limit=args.limit, rescore=args.rescore
        )
    finally:
        conn.close()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    try:
        jobs = db.list_jobs(
            conn,
            status=args.status,
            q=args.query,
            min_score=args.min_score,
            limit=args.limit,
        )
    finally:
        conn.close()
    _print_table(jobs)
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    from .prepare import prepare_job

    cfg = load_config()
    try:
        prepare_job(
            args.job,
            cfg=cfg,
            force=args.force,
            cover_letter=False if args.no_cover_letter else None,
        )
    except ConfigError as exc:
        print(f"\nCannot prepare: {exc}\n", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI should explain failures
        print(f"\nPreparation failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    from .autofill import run_autofill

    cfg = load_config()
    try:
        run_autofill(
            args.job,
            cfg=cfg,
            dry_run=args.dry_run,
            click_apply=False if args.no_click_apply else None,
            headless=True if args.headless else None,
            keep_open=not args.close_after,
            prepare_if_missing=args.prepare,
        )
    except ConfigError as exc:
        print(f"\nCannot autofill: {exc}\n", file=sys.stderr)
        return 1
    except ImportError:
        print(
            "\nPlaywright is not installed. Run:\n"
            "  pip install -r requirements.txt\n"
            "  playwright install chromium\n",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nAutofill failed: {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return 1
    return 0


def cmd_mark(args: argparse.Namespace) -> int:
    cfg = load_config()
    conn = db.connect(cfg)
    try:
        job = db.resolve_job(conn, args.job)
        if not job:
            print(f"No job found matching {args.job!r}.", file=sys.stderr)
            return 1
        db.set_status(conn, int(job["id"]), args.status, note=args.note)
        print(
            f"Marked #{job['id']} {job.get('title')} @ {job.get('company')} "
            f"as {args.status}."
        )
    finally:
        conn.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config()
    _write_report(cfg, include_all=args.all)
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    from .dashboard import serve_dashboard

    cfg = load_config()
    serve_dashboard(
        cfg=cfg,
        host=args.host,
        port=args.port,
        open_browser=not args.no_open,
    )
    return 0


def cmd_import_resume(args: argparse.Namespace) -> int:
    from .importer import import_resume

    try:
        import_resume(args.file, out_path=args.out, force=args.force)
    except ConfigError as exc:
        print(f"\nImport failed: {exc}\n", file=sys.stderr)
        return 1
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    print("JobHunt doctor\n")
    failures = 0

    _ok(f"Python {sys.version.split()[0]}")

    try:
        cfg = load_config()
        _ok(f"config.yaml loaded ({cfg['_config_path']})")
    except ConfigError as exc:
        _fail(str(exc))
        return 1

    try:
        resume = load_resume()
        issues = placeholder_issues(resume)
        if issues:
            _warn("master resume needs editing:")
            for issue in issues:
                print(f"         - {issue}")
        else:
            _ok(f"master resume ready ({resume.get('name')})")
    except ConfigError as exc:
        _fail(str(exc))
        failures += 1

    try:
        bank = load_answer_bank()
        if bank:
            _ok(f"answer bank loaded ({len(bank)} entries)")
        else:
            _warn("answer bank is empty (data/answers.yaml)")
    except Exception as exc:  # noqa: BLE001
        _warn(f"answer bank could not be loaded: {exc}")

    try:
        conn = db.connect(cfg)
        try:
            s = db.stats(conn)
            _ok(
                f"database ready: {s['total']} jobs, {s['scored']} scored, "
                f"{s['by_status'].get('applied', 0)} applied"
            )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        _fail(f"database error: {exc}")
        failures += 1

    print("\n" + llm_status(cfg))

    modules = [
        ("jobspy", "job discovery"),
        ("playwright", "browser autofill + PDF"),
        ("docx", "DOCX resumes"),
        ("jinja2", "resume templates"),
        ("fastapi", "dashboard"),
        ("uvicorn", "dashboard server"),
        ("yaml", "config files"),
        ("pypdf", "PDF resume import"),
    ]
    for module, purpose in modules:
        if importlib.util.find_spec(module):
            _ok(f"{module} installed ({purpose})")
        else:
            _warn(f"{module} missing ({purpose}) - pip install -r requirements.txt")

    if importlib.util.find_spec("playwright"):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            _ok("Playwright Chromium is installed")
        except Exception as exc:  # noqa: BLE001
            _warn(f"Playwright Chromium unavailable: {str(exc)[:160]}")
            print("         Run: playwright install chromium")

    try:
        out = output_dir(cfg)
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        _ok(f"output directory writable: {out}")
    except Exception as exc:  # noqa: BLE001
        _fail(f"output directory is not writable: {exc}")
        failures += 1

    print()
    if failures:
        print(
            f"{failures} hard problem(s) found. Fix them before running the pipeline."
        )
    else:
        print(
            "Ready. Next: `python jobhunt.py run`, then `python jobhunt.py dashboard`."
        )
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunt",
        description="Job discovery -> scoring -> tailored resume -> autofill -> tracking.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Typical flow:
              python jobhunt.py doctor
              python jobhunt.py run --prepare-top 3
              python jobhunt.py dashboard
              python jobhunt.py apply 12
              python jobhunt.py mark 12 applied

            The autofill command never clicks Submit. You review and submit.
            """
        ),
    )
    parser.add_argument("--version", action="version", version="JobHunt 2.0.0")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser(
        "run", help="discover, score, report, optionally prepare top jobs"
    )
    p.add_argument("--no-discover", action="store_true")
    p.add_argument("--no-score", action="store_true")
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--limit", type=int, default=None, help="max jobs to score this run")
    p.add_argument(
        "--rescore", action="store_true", help="re-score jobs that already have a score"
    )
    p.add_argument(
        "--prepare-top", type=int, default=0, help="prepare the top N un-prepared jobs"
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("discover", help="search job boards only")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("score", help="score unscored jobs")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--rescore", action="store_true")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("list", help="list tracked jobs")
    p.add_argument("--status", choices=db.STATUSES + ["active"], default=None)
    p.add_argument("--query", "-q", default=None)
    p.add_argument("--min-score", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser(
        "prepare", help="tailor resume + cover letter + screening answers"
    )
    p.add_argument("job", help="job id, URL, or title fragment")
    p.add_argument(
        "--force", action="store_true", help="allow an unedited template resume"
    )
    p.add_argument("--no-cover-letter", action="store_true")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("apply", help="open browser and autofill (never submits)")
    p.add_argument("job", help="job id, URL, or title fragment")
    p.add_argument(
        "--dry-run", action="store_true", help="show detected fields without filling"
    )
    p.add_argument(
        "--no-click-apply",
        action="store_true",
        help="do not click an Apply button first",
    )
    p.add_argument(
        "--headless", action="store_true", help="run without a visible browser"
    )
    p.add_argument(
        "--close-after",
        action="store_true",
        help="close browser immediately after filling",
    )
    p.add_argument(
        "--prepare",
        action="store_true",
        help="generate the application first if missing",
    )
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("mark", help="update application status")
    p.add_argument("job", help="job id, URL, or title fragment")
    p.add_argument("status", choices=db.STATUSES)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("report", help="write report.csv")
    p.add_argument(
        "--all", action="store_true", help="include jobs below the report threshold"
    )
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("dashboard", help="start the local web dashboard")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-open", action="store_true")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("import-resume", help="extract a PDF/DOCX resume into YAML")
    p.add_argument("file")
    p.add_argument("--out", default=None)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_import_resume)

    p = sub.add_parser("doctor", help="check configuration, dependencies and browser")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
