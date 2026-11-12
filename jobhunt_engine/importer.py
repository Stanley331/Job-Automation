"""Import an existing PDF/DOCX resume into the structured master-resume YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from .config import ROOT, ConfigError, load_config
from .llm import LLM, LLMError

IMPORT_PROMPT = """Extract a structured resume from the text below.

Rules:
- Only extract facts that appear in the text. Never invent or embellish.
- Keep bullets close to the original wording; fix obvious OCR line breaks.
- If a field is missing, use an empty string or an empty list.
- Skills must be grouped into sensible categories that already appear in the text.
- Return ONLY valid JSON with this exact shape:
{{
  "name": "", "headline": "", "email": "", "phone": "", "location": "",
  "links": {{"linkedin": "", "github": "", "portfolio": ""}},
  "summary": "",
  "skills": [{{"category": "", "items": []}}],
  "experience": [
    {{"company": "", "title": "", "location": "", "start": "", "end": "", "bullets": []}}
  ],
  "projects": [{{"name": "", "tech": "", "bullets": []}}],
  "education": [{{"degree": "", "institution": "", "year": ""}}],
  "certifications": []
}}

RESUME TEXT:
{text}
"""


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ConfigError(
                "Install pypdf to import PDFs: pip install pypdf"
            ) from exc
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix in {".docx", ".doc"}:
        try:
            from docx import Document
        except ImportError as exc:
            raise ConfigError("Install python-docx to import DOCX files") from exc
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts)
    if suffix in {".txt", ".md", ".markdown"}:
        return path.read_text(encoding="utf-8", errors="replace")
    raise ConfigError(f"Unsupported resume format: {suffix}. Use PDF, DOCX, TXT or MD.")


def _normalise(data: dict) -> dict:
    def s(value) -> str:
        return str(value or "").strip()

    def items(value) -> list:
        return value if isinstance(value, list) else []

    out = {
        "name": s(data.get("name")),
        "headline": s(data.get("headline")),
        "email": s(data.get("email")),
        "phone": s(data.get("phone")),
        "location": s(data.get("location")),
        "links": data.get("links") if isinstance(data.get("links"), dict) else {},
        "summary": s(data.get("summary")),
        "skills": [],
        "experience": [],
        "projects": [],
        "education": [],
        "certifications": [s(c) for c in items(data.get("certifications")) if s(c)],
    }
    for group in items(data.get("skills")):
        if isinstance(group, dict):
            entries = [s(i) for i in items(group.get("items")) if s(i)]
            if entries:
                out["skills"].append(
                    {"category": s(group.get("category")) or "Skills", "items": entries}
                )
    for exp in items(data.get("experience")):
        if not isinstance(exp, dict):
            continue
        out["experience"].append(
            {
                "company": s(exp.get("company")),
                "title": s(exp.get("title")),
                "location": s(exp.get("location")),
                "start": s(exp.get("start")),
                "end": s(exp.get("end")),
                "bullets": [s(b) for b in items(exp.get("bullets")) if s(b)],
            }
        )
    for project in items(data.get("projects")):
        if isinstance(project, dict):
            out["projects"].append(
                {
                    "name": s(project.get("name")),
                    "tech": s(project.get("tech")),
                    "bullets": [s(b) for b in items(project.get("bullets")) if s(b)],
                }
            )
    for edu in items(data.get("education")):
        if isinstance(edu, dict):
            out["education"].append(
                {
                    "degree": s(edu.get("degree")),
                    "institution": s(edu.get("institution")),
                    "year": s(edu.get("year")),
                }
            )
    return out


def import_resume(
    path: str | Path,
    out_path: str | Path | None = None,
    force: bool = False,
) -> Path:
    cfg = load_config()
    llm = LLM(cfg)
    if not llm.available:
        raise ConfigError(
            "Resume import needs an LLM. Set GEMINI_API_KEY in .env, "
            "or fill data/master_resume.yaml by hand."
        )
    src = Path(path)
    if not src.exists():
        raise ConfigError(f"Resume file not found: {src}")
    text = extract_text(src).strip()
    if len(text) < 80:
        raise ConfigError(
            "Could not extract enough text from the resume. Try a text-based PDF or DOCX."
        )
    print(f"Extracting structure with {llm.describe()}...")
    try:
        data = llm.generate_json(IMPORT_PROMPT.format(text=text[:24000]))
    except LLMError as exc:
        raise ConfigError(f"LLM import failed: {exc}") from exc

    resume = _normalise(data)
    if not resume["name"] or not resume["experience"]:
        raise ConfigError(
            "The LLM could not extract a name and work experience. "
            "Try a cleaner resume file, or edit data/master_resume.yaml manually."
        )
    dest = Path(out_path) if out_path else ROOT / "data" / "master_resume.imported.yaml"
    if dest.exists() and not force:
        raise ConfigError(
            f"{dest} already exists. Review it, then rerun with --force to overwrite, "
            "or pass --out another-file.yaml."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Imported from "
        + str(src)
        + "\n# REVIEW THIS FILE, then copy it over data/master_resume.yaml.\n"
        + "# The pipeline blocks preparation until placeholder names/emails are gone.\n\n"
    )
    dest.write_text(
        header + yaml.safe_dump(resume, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"Wrote {dest}")
    print(
        "Review it, then replace data/master_resume.yaml with it (or copy the contents)."
    )
    return dest
