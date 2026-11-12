"""Configuration, resume and answer-bank loading.

All user-facing data lives in plain YAML files at the project root so it is
easy to edit by hand and diff over time.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

import yaml

try:  # optional, but recommended
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(ROOT / ".env")


class ConfigError(Exception):
    """Raised when a config/resume file is missing or unusable."""


DEFAULT_CONFIG: dict[str, Any] = {
    "candidate": {
        "full_name": "",
        "email": "",
        "phone": "",
        "location": "",
        "city": "",
        "country": "India",
        "linkedin": "",
        "github": "",
        "portfolio": "",
        "years_experience": 0,
        "current_title": "",
        "current_company": "",
        "notice_period": "",
        "expected_ctc": "",
        "current_ctc": "",
        "work_authorization": "",
        "willing_relocate": "",
        "availability": "",
        "preferred_titles": [],
        "locations_ok": [],
        "min_salary_lpa": 0,
        "avoid": [],
        "profile_notes": "",
    },
    "search": {
        "terms": ["java developer", "spring boot developer"],
        "locations": [{"location": "Delhi", "remote": False}],
        "sites": ["naukri", "indeed"],
        "results_per_search": 30,
        "hours_old": 72,
        "country_indeed": "India",
        "sleep_between_searches": 3,
    },
    "scoring": {
        "min_score_for_report": 60,
        "max_per_run": 80,
        "keyword_bonus": True,
    },
    "llm": {
        "provider": "auto",
        "gemini_model": "gemini-2.5-flash",
        "openai_model": "gpt-4o-mini",
        "openai_base_url": "https://api.openai.com/v1",
        "temperature": 0.3,
        "min_seconds_between_calls": 6,
        "max_retries": 3,
    },
    "output": {
        "dir": "output",
        "report_path": "output/report.csv",
        "generate_cover_letter": True,
    },
    "autofill": {
        "browser_profile": ".browser/profile",
        "headless": False,
        "click_apply": True,
        "slow_mo_ms": 40,
        "auto_check_consent": False,
        "answer_demographics": False,
    },
    "dashboard": {"host": "127.0.0.1", "port": 8765},
    "database": "data/jobs.db",
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_yaml(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or default
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config.yaml"
    if not p.exists():
        raise ConfigError(
            f"Missing {p}. Copy config.example.yaml to config.yaml and edit it."
        )
    cfg = _deep_merge(DEFAULT_CONFIG, _read_yaml(p, {}))

    # Environment overrides (kept small and explicit).
    provider = os.getenv("JOBHUNT_LLM_PROVIDER")
    if provider:
        cfg["llm"]["provider"] = provider
    if os.getenv("GEMINI_MODEL"):
        cfg["llm"]["gemini_model"] = os.environ["GEMINI_MODEL"]
    if os.getenv("OPENAI_MODEL"):
        cfg["llm"]["openai_model"] = os.environ["OPENAI_MODEL"]
    if os.getenv("OPENAI_BASE_URL"):
        cfg["llm"]["openai_base_url"] = os.environ["OPENAI_BASE_URL"]
    if os.getenv("JOBHUNT_DB"):
        cfg["database"] = os.environ["JOBHUNT_DB"]

    cfg["_root"] = str(ROOT)
    cfg["_config_path"] = str(p)
    return cfg


def resolve_path(cfg: dict, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else Path(cfg.get("_root", ROOT)) / p


def db_path(cfg: dict) -> Path:
    return resolve_path(cfg, cfg["database"])


def output_dir(cfg: dict) -> Path:
    return resolve_path(cfg, cfg["output"]["dir"])


def report_path(cfg: dict) -> Path:
    return resolve_path(cfg, cfg["output"]["report_path"])


def ensure_dirs(cfg: dict) -> None:
    out = output_dir(cfg)
    for sub in (out, out / "resumes", out / "cover_letters"):
        sub.mkdir(parents=True, exist_ok=True)
    db_path(cfg).parent.mkdir(parents=True, exist_ok=True)


def load_resume(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "data" / "master_resume.yaml"
    data = _read_yaml(p, None)
    if not isinstance(data, dict):
        raise ConfigError(
            f"Missing or invalid master resume: {p}. "
            "Copy data/master_resume.example.yaml to data/master_resume.yaml "
            "and edit it."
        )
    data.setdefault("skills", [])
    data.setdefault("experience", [])
    data.setdefault("projects", [])
    data.setdefault("education", [])
    data.setdefault("certifications", [])
    data.setdefault("links", {})
    return data


def load_answer_bank(path: str | Path | None = None) -> list[dict]:
    p = Path(path) if path else ROOT / "data" / "answers.yaml"
    data = _read_yaml(p, {})
    if isinstance(data, dict):
        entries = data.get("answers", [])
    elif isinstance(data, list):
        entries = data
    else:
        entries = []
    return [e for e in entries if isinstance(e, dict)]


def split_name(full_name: str) -> tuple[str, str]:
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _latest_experience(resume: dict) -> dict:
    exp = resume.get("experience") or []
    return exp[0] if exp and isinstance(exp[0], dict) else {}


def flat_candidate(cfg: dict, resume: dict | None = None) -> dict[str, str]:
    """Flatten config + resume into values used for form filling."""
    c = cfg.get("candidate", {})
    resume = resume or {}
    links = resume.get("links") or {}
    latest = _latest_experience(resume)
    name = resume.get("name") or c.get("full_name") or ""
    first, last = split_name(name)

    def pick(*values: Any) -> str:
        for v in values:
            if v is not None and str(v).strip():
                return str(v).strip()
        return ""

    facts = {
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "email": pick(resume.get("email"), c.get("email")),
        "phone": pick(resume.get("phone"), c.get("phone")),
        "location": pick(resume.get("location"), c.get("location")),
        "city": pick(c.get("city"), c.get("location")),
        "country": pick(c.get("country")),
        "linkedin": pick(links.get("linkedin"), c.get("linkedin")),
        "github": pick(links.get("github"), c.get("github")),
        "portfolio": pick(links.get("portfolio"), c.get("portfolio")),
        "years_experience": str(c.get("years_experience") or ""),
        "current_title": pick(c.get("current_title"), latest.get("title")),
        "current_company": pick(c.get("current_company"), latest.get("company")),
        "notice_period": pick(c.get("notice_period")),
        "expected_ctc": pick(c.get("expected_ctc")),
        "current_ctc": pick(c.get("current_ctc")),
        "work_authorization": pick(c.get("work_authorization")),
        "willing_relocate": pick(c.get("willing_relocate")),
        "availability": pick(c.get("availability")),
    }
    return {k: v for k, v in facts.items() if v}


def placeholder_issues(resume: dict) -> list[str]:
    """Detect an unedited template. Preparation is blocked until these are fixed."""
    issues: list[str] = []
    name = str(resume.get("name") or "").strip()
    email = str(resume.get("email") or "").strip()
    if not name or name.lower() in {"your name", "name"}:
        issues.append("resume name is still a placeholder")
    if not email or "example.com" in email.lower():
        issues.append("resume email is still a placeholder")
    if not resume.get("experience"):
        issues.append("resume has no work experience entries")
    for exp in resume.get("experience") or []:
        company = str(exp.get("company") or "").strip()
        if company.lower() in {"company one", "company two", "your current company"}:
            issues.append(f"experience company {company!r} is still a placeholder")
    return issues


def candidate_profile_text(cfg: dict, resume: dict) -> str:
    """A compact profile used by scoring/tailoring prompts."""
    c = cfg.get("candidate", {})
    skills: list[str] = []
    for group in resume.get("skills") or []:
        items = group.get("items") or []
        if items:
            skills.append(
                f"{group.get('category', 'Skills')}: {', '.join(map(str, items))}"
            )
    latest = _latest_experience(resume)
    lines = [
        f"Name: {resume.get('name') or c.get('full_name', '')}",
        f"Headline: {resume.get('headline', '')}",
        f"Experience: {c.get('years_experience', '')} years",
        f"Current role: {latest.get('title', '')} at {latest.get('company', '')}",
        f"Location: {resume.get('location') or c.get('location', '')}",
        f"Summary: {(resume.get('summary') or '').strip()}",
        "Skills:",
        *[f"  - {s}" for s in skills],
        "Preferences:",
        f"  - Target titles: {', '.join(map(str, c.get('preferred_titles') or []))}",
        f"  - Locations: {', '.join(map(str, c.get('locations_ok') or []))}",
        f"  - Minimum salary: {c.get('min_salary_lpa', '')} LPA",
        f"  - Avoid: {', '.join(map(str, c.get('avoid') or []))}",
    ]
    notes = str(c.get("profile_notes") or "").strip()
    if notes:
        lines += ["Notes:", notes]
    return "\n".join(lines)


def sanitize_slug(value: str, max_len: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()
    return slug[:max_len].strip("-") or "untitled"
