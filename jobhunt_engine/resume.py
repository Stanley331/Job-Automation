"""Tailored resume + cover letter generation.

The LLM may select, re-order and rephrase content from the master resume, but
validation prevents invented employers, skills, dates or metrics from being
rendered.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import (
    candidate_profile_text,
    output_dir,
    sanitize_slug,
)
from .llm import LLM, LLMError

TEMPLATES = Path(__file__).resolve().parent / "templates"
ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATES)),
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

TAILOR_PROMPT = """You are tailoring an existing resume to a specific job.

CANDIDATE PROFILE:
{profile}

MASTER RESUME (the only source of truth):
{master}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Produce a tailored version. Rules:
- NEVER invent employers, job titles, dates, degrees, certifications, skills,
  tools, metrics, team sizes or achievements.
- Keep every job in the master resume. You may select and rephrase bullets.
- Keep 3-5 of the most relevant bullets per role; preserve real metrics.
- Re-order skills within categories by relevance. Only use skills that already
  appear in the master resume.
- Rewrite the summary in 2-3 sentences for this role.
- Keep it truthful and ATS-friendly; no tables, columns or graphics.

Return ONLY valid JSON:
{{
  "headline": "target headline",
  "summary": "tailored summary",
  "skills": [{{"category": "Languages", "items": ["Java", "SQL"]}}],
  "experience": [{{"company": "exact company name", "bullets": ["..."]}}],
  "projects": [{{"name": "exact project name", "bullets": ["..."]}}],
  "keywords": ["relevant keywords from the posting"]
}}
"""

COVER_PROMPT = """Write a concise cover letter for this application.

CANDIDATE:
{profile}

MASTER RESUME:
{resume}

TAILORED RESUME SUMMARY:
{summary}

JOB:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

Rules:
- 3 short paragraphs, at most 250 words.
- Mention the role and company, and 1-2 concrete achievements from the resume.
- First person, specific, no fluff, no placeholders, no invented facts.
- Do not repeat the resume bullet-for-bullet.
- Return plain text only, with paragraphs separated by blank lines.
"""

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]{2,}", (text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _job_keywords(job: dict, cfg: dict) -> set[str]:
    c = cfg.get("candidate", {})
    text = " ".join(
        [
            str(job.get("title") or ""),
            str(job.get("description") or ""),
            " ".join(map(str, c.get("preferred_titles") or [])),
            " ".join(map(str, c.get("profile_notes") or [])),
        ]
    )
    return _tokens(text)


def _offline_tailor(job: dict, master: dict, cfg: dict) -> dict:
    keywords = _job_keywords(job, cfg)
    experience = []
    for exp in master.get("experience") or []:
        bullets = [str(b) for b in (exp.get("bullets") or []) if str(b).strip()]
        scored = sorted(
            enumerate(bullets),
            key=lambda pair: (-len(_tokens(pair[1]) & keywords), pair[0]),
        )
        keep = [b for _, b in scored[:4]]
        if len(keep) < min(3, len(bullets)):
            keep = bullets[: max(3, len(keep))]
        entry = dict(exp)
        entry["bullets"] = keep
        experience.append(entry)

    skills = []
    for group in master.get("skills") or []:
        items = [str(i) for i in (group.get("items") or [])]
        items.sort(key=lambda i: (len(_tokens(i) & keywords), len(items)), reverse=True)
        skills.append({"category": group.get("category"), "items": items})

    projects = []
    for project in master.get("projects") or []:
        bullets = [str(b) for b in (project.get("bullets") or []) if str(b).strip()]
        bullets.sort(key=lambda b: len(_tokens(b) & keywords), reverse=True)
        entry = dict(project)
        entry["bullets"] = bullets[:3]
        projects.append(entry)
    projects.sort(key=lambda p: len(_tokens(json.dumps(p)) & keywords), reverse=True)

    return {
        "headline": master.get("headline", ""),
        "summary": (master.get("summary") or "").strip(),
        "skills": skills,
        "experience": experience,
        "projects": projects[:3],
        "keywords": sorted(keywords)[:30],
    }


def _clean_bullets(
    value: Any, source_bullets: list[str], warnings: list[str], label: str
) -> list[str]:
    if not isinstance(value, list):
        return source_bullets
    master_text = " ".join(source_bullets)
    master_numbers = set(NUMBER_RE.findall(master_text))
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        bullet = re.sub(r"\s+", " ", raw).strip(" -•\t")
        if not bullet or len(bullet) > 400:
            continue
        # Metrics must already exist in the master resume.
        numbers = set(NUMBER_RE.findall(bullet))
        if not numbers.issubset(master_numbers):
            warnings.append(f"{label}: dropped bullet with new metric: {bullet[:120]}")
            continue
        overlap = max(
            (_jaccard(_tokens(bullet), _tokens(src)) for src in source_bullets),
            default=0.0,
        )
        if overlap < 0.18:
            warnings.append(
                f"{label}: dropped bullet with weak grounding: {bullet[:120]}"
            )
            continue
        out.append(bullet)
    return out[:6] or source_bullets


def validate_tailoring(master: dict, data: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    if not isinstance(data, dict):
        return _offline_tailor({}, master, {"candidate": {}}), [
            "LLM returned invalid tailoring JSON"
        ]

    summary = str(data.get("summary") or "").strip()
    master_summary = str(master.get("summary") or "").strip()
    if summary:
        master_all = json.dumps(master, ensure_ascii=False)
        if not set(NUMBER_RE.findall(summary)).issubset(
            set(NUMBER_RE.findall(master_all))
        ):
            warnings.append(
                "summary contained a new number; using master summary instead"
            )
            summary = master_summary
        elif len(summary) > 1200:
            summary = summary[:1200].rsplit(" ", 1)[0]
    else:
        summary = master_summary

    # Skills must already exist in the master resume.
    known: dict[str, str] = {}
    for group in master.get("skills") or []:
        for item in group.get("items") or []:
            known[str(item).strip().lower()] = str(item).strip()
    skills: list[dict] = []
    for group in data.get("skills") or []:
        if not isinstance(group, dict):
            continue
        items: list[str] = []
        for item in group.get("items") or []:
            original = known.get(str(item).strip().lower())
            if original and original not in items:
                items.append(original)
            elif not original:
                warnings.append(f"skills: dropped unknown skill {item!r}")
        if items:
            skills.append(
                {"category": str(group.get("category") or "Skills"), "items": items}
            )
    if not skills:
        skills = [
            {"category": g.get("category"), "items": list(g.get("items") or [])}
            for g in master.get("skills") or []
        ]

    # Keep every master role; use tailored bullets only where grounded.
    exp_by_company = {}
    for exp in data.get("experience") or []:
        if isinstance(exp, dict) and exp.get("company"):
            exp_by_company[str(exp["company"]).strip().lower()] = exp
    experience: list[dict] = []
    for exp in master.get("experience") or []:
        company = str(exp.get("company") or "")
        tailored = exp_by_company.get(company.strip().lower(), {})
        source_bullets = [str(b) for b in (exp.get("bullets") or []) if str(b).strip()]
        bullets = _clean_bullets(
            tailored.get("bullets"), source_bullets, warnings, f"experience/{company}"
        )
        merged = dict(exp)
        merged["bullets"] = bullets
        experience.append(merged)

    project_by_name = {}
    for project in data.get("projects") or []:
        if isinstance(project, dict) and project.get("name"):
            project_by_name[str(project["name"]).strip().lower()] = project
    projects: list[dict] = []
    for project in master.get("projects") or []:
        name = str(project.get("name") or "")
        tailored = project_by_name.get(name.strip().lower())
        if not tailored:
            continue
        source_bullets = [
            str(b) for b in (project.get("bullets") or []) if str(b).strip()
        ]
        merged = dict(project)
        merged["bullets"] = _clean_bullets(
            tailored.get("bullets"), source_bullets, warnings, f"project/{name}"
        )
        projects.append(merged)
    if not projects:
        projects = list(master.get("projects") or [])

    headline = str(data.get("headline") or "").strip()[:140] or str(
        master.get("headline") or ""
    )
    return {
        "headline": headline,
        "summary": summary,
        "skills": skills,
        "experience": experience,
        "projects": projects[:3],
        "keywords": [str(k) for k in (data.get("keywords") or [])][:30],
    }, warnings


def tailor_resume(
    job: dict, master: dict, cfg: dict, llm: LLM
) -> tuple[dict, list[str]]:
    if not llm.available:
        return _offline_tailor(job, master, cfg), []
    prompt = TAILOR_PROMPT.format(
        profile=candidate_profile_text(cfg, master),
        master=json.dumps(master, ensure_ascii=False)[:14000],
        title=job.get("title") or "",
        company=job.get("company") or "",
        location=job.get("location") or "",
        description=(job.get("description") or "")[:6000],
    )
    try:
        data = llm.generate_json(prompt)
    except LLMError as exc:
        return _offline_tailor(job, master, cfg), [
            f"LLM tailoring failed, used offline selection: {exc}"
        ]
    return validate_tailoring(master, data)


def _merged_resume(master: dict, tailored: dict) -> dict:
    merged = dict(master)
    merged.update(
        {
            "headline": tailored.get("headline") or master.get("headline", ""),
            "summary": tailored.get("summary") or master.get("summary", ""),
            "skills": tailored.get("skills") or master.get("skills") or [],
            "experience": tailored.get("experience") or master.get("experience") or [],
            "projects": tailored.get("projects") or master.get("projects") or [],
            "education": master.get("education") or [],
            "certifications": master.get("certifications") or [],
        }
    )
    return merged


def _contact_parts(resume: dict) -> list[str]:
    links = resume.get("links") or {}
    parts = [
        resume.get("email"),
        resume.get("phone"),
        resume.get("location"),
        links.get("linkedin"),
        links.get("github"),
        links.get("portfolio"),
    ]
    return [str(p).strip() for p in parts if p and str(p).strip()]


def _display_link(value: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", value).rstrip("/")


def html_to_pdf(html: str, path: Path) -> tuple[Path | None, str | None]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "playwright is not installed; PDF skipped (HTML/DOCX were written)"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            page.emulate_media(media="print")
            page.pdf(
                path=str(path),
                format="A4",
                print_background=True,
                margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
            )
            browser.close()
        return path, None
    except Exception as exc:  # noqa: BLE001 - missing browser is common
        return None, f"PDF rendering failed ({exc}); HTML/DOCX were written"


def _write_docx(resume: dict, path: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.6)
    section.bottom_margin = Inches(0.6)
    section.left_margin = Inches(0.65)
    section.right_margin = Inches(0.65)
    styles = doc.styles
    styles["Normal"].font.name = "Calibri"
    styles["Normal"].font.size = Pt(10.5)

    name = doc.add_paragraph()
    name.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = name.add_run(str(resume.get("name") or ""))
    run.bold = True
    run.font.size = Pt(21)
    run.font.color.rgb = RGBColor(0x11, 0x11, 0x11)

    headline = doc.add_paragraph()
    headline.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hrun = headline.add_run(str(resume.get("headline") or ""))
    hrun.font.size = Pt(10.5)
    hrun.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    contact = doc.add_paragraph()
    contact.alignment = WD_ALIGN_PARAGRAPH.CENTER
    crun = contact.add_run(" | ".join(_contact_parts(resume)))
    crun.font.size = Pt(9)
    crun.font.color.rgb = RGBColor(0x44, 0x44, 0x44)

    def heading(text: str) -> None:
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(10)
        p.paragraph_format.space_after = Pt(4)
        r = p.add_run(text.upper())
        r.bold = True
        r.font.size = Pt(11)
        r.font.color.rgb = RGBColor(0x11, 0x11, 0x11)

    if resume.get("summary"):
        heading("Summary")
        doc.add_paragraph(str(resume["summary"]).strip())

    if resume.get("skills"):
        heading("Skills")
        for group in resume["skills"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(f"{group.get('category', 'Skills')}: ")
            r.bold = True
            p.add_run(", ".join(map(str, group.get("items") or [])))

    if resume.get("experience"):
        heading("Experience")
        for exp in resume["experience"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(1)
            r = p.add_run(str(exp.get("title") or ""))
            r.bold = True
            dates = " - ".join(
                x for x in [str(exp.get("start") or ""), str(exp.get("end") or "")] if x
            )
            p.add_run(f"   {dates}")
            sub = doc.add_paragraph()
            sub.paragraph_format.space_after = Pt(2)
            sr = sub.add_run(str(exp.get("company") or ""))
            sr.italic = True
            if exp.get("location"):
                sub.add_run(f" | {exp['location']}")
            for bullet in exp.get("bullets") or []:
                doc.add_paragraph(str(bullet), style="List Bullet")

    if resume.get("projects"):
        heading("Projects")
        for project in resume["projects"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(1)
            r = p.add_run(str(project.get("name") or ""))
            r.bold = True
            if project.get("tech"):
                p.add_run(f" | {project['tech']}")
            for bullet in project.get("bullets") or []:
                doc.add_paragraph(str(bullet), style="List Bullet")

    if resume.get("education"):
        heading("Education")
        for edu in resume["education"]:
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(1)
            r = p.add_run(str(edu.get("degree") or ""))
            r.bold = True
            p.add_run(f" - {edu.get('institution', '')}")
            if edu.get("year"):
                p.add_run(f" ({edu['year']})")

    if resume.get("certifications"):
        heading("Certifications")
        for cert in resume["certifications"]:
            doc.add_paragraph(str(cert), style="List Bullet")

    doc.save(str(path))


def _artifact_base(job: dict) -> str:
    return (
        f"job{job.get('id', 'x')}_"
        f"{sanitize_slug(str(job.get('company') or 'company'), 30)}_"
        f"{sanitize_slug(str(job.get('title') or 'role'), 36)}"
    )


def _rel(path: Path, cfg: dict) -> str:
    try:
        return path.relative_to(output_dir(cfg)).as_posix()
    except ValueError:
        return str(path)


def render_resume(job: dict, master: dict, tailored: dict, cfg: dict) -> dict:
    resume = _merged_resume(master, tailored)
    html = ENV.get_template("resume.html").render(
        resume=resume,
        contact_parts=_contact_parts(resume),
        display_link=_display_link,
        generated_for={
            "title": job.get("title") or "",
            "company": job.get("company") or "",
        },
    )
    base = _artifact_base(job)
    out = output_dir(cfg) / "resumes"
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{base}.html"
    docx_path = out / f"{base}.docx"
    pdf_path = out / f"{base}.pdf"
    html_path.write_text(html, encoding="utf-8")
    _write_docx(resume, docx_path)
    pdf, pdf_warning = html_to_pdf(html, pdf_path)
    return {
        "html": _rel(html_path, cfg),
        "docx": _rel(docx_path, cfg),
        "pdf": _rel(pdf, cfg) if pdf else None,
        "warnings": [pdf_warning] if pdf_warning else [],
    }


def write_cover_letter(
    job: dict, master: dict, tailored: dict, cfg: dict, llm: LLM
) -> tuple[str, list[str], str]:
    warnings: list[str] = []
    if llm.available:
        prompt = COVER_PROMPT.format(
            profile=candidate_profile_text(cfg, master),
            resume=json.dumps(master, ensure_ascii=False)[:10000],
            summary=tailored.get("summary") or "",
            title=job.get("title") or "",
            company=job.get("company") or "",
            location=job.get("location") or "",
            description=(job.get("description") or "")[:6000],
        )
        try:
            text = llm.generate(prompt).strip()
            if re.search(r"\[[^\]]+\]|\bTODO\b", text):
                warnings.append(
                    "cover letter contains a placeholder; review it before sending"
                )
            if len(text.split()) > 320:
                warnings.append("cover letter is longer than 250 words")
            return text, warnings, f"llm:{llm.model}"
        except LLMError as exc:
            warnings.append(f"LLM cover letter failed, used template: {exc}")

    exp = (master.get("experience") or [{}])[0]
    skills = []
    for group in master.get("skills") or []:
        skills.extend(map(str, group.get("items") or []))
    top_skills = ", ".join(skills[:4]) or "backend development"
    text = (
        f"Dear Hiring Manager,\n\n"
        f"I am writing to apply for the {job.get('title') or 'role'} position at "
        f"{job.get('company') or 'your company'}. With {cfg['candidate'].get('years_experience', '')} "
        f"years of experience in {top_skills}, I have built and shipped backend services "
        f"that are used in production and care deeply about reliability and clear ownership.\n\n"
        f"In my role as {exp.get('title', 'engineer')} at {exp.get('company', 'my current company')}, "
        f"I have worked across API design, data modelling and production debugging. "
        f"{str((exp.get('bullets') or [''])[0]).strip()}\n\n"
        f"I would welcome the chance to discuss how I can contribute to "
        f"{job.get('company') or 'your team'}. Thank you for your time and consideration.\n\n"
        f"Sincerely,\n{master.get('name') or ''}"
    )
    warnings.append("offline template cover letter - review the wording before sending")
    return text, warnings, "template"


def render_cover_letter(job: dict, master: dict, text: str, cfg: dict) -> dict:
    html = ENV.get_template("cover_letter.html").render(
        resume=master,
        contact_parts=_contact_parts(master),
        display_link=_display_link,
        body=text,
        date_str=date.today().strftime("%d %B %Y"),
        company=job.get("company") or "",
        title=job.get("title") or "",
    )
    base = _artifact_base(job)
    out = output_dir(cfg) / "cover_letters"
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{base}.html"
    pdf_path = out / f"{base}.pdf"
    html_path.write_text(html, encoding="utf-8")
    pdf, warning = html_to_pdf(html, pdf_path)
    return {
        "html": _rel(html_path, cfg),
        "pdf": _rel(pdf, cfg) if pdf else None,
        "warnings": [warning] if warning else [],
    }
