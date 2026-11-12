"""Score jobs against the candidate profile with an LLM or offline heuristics."""

from __future__ import annotations

import re

from . import db
from .config import candidate_profile_text
from .llm import LLM, LLMError

PROMPT = """You are screening job postings for a candidate.

CANDIDATE PROFILE:
{profile}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Remote: {remote}
Listed salary: {salary}
Description:
{description}

Score how well this job fits the candidate. Consider role match, seniority,
location/remote, salary (if stated), and red flags. Be strict: a clearly
different role, a much more senior role, or a location the candidate cannot
commute to should score below 30.

Return ONLY valid JSON with these keys:
- score: integer 0-100
- reason: one or two sentences
- red_flags: short string, or "" if none
- salary_ok: "yes", "no" or "unknown" against the candidate's minimum salary

Do not invent facts about the candidate. Return JSON only."""

POSITIVE_TITLE = [
    "java",
    "spring",
    "backend",
    "back end",
    "back-end",
    "software engineer",
    "software development engineer",
    "sde",
    "microservice",
    "api",
]
POSITIVE_BODY = [
    "java",
    "spring boot",
    "spring",
    "microservice",
    "rest api",
    "postgres",
    "postgresql",
    "mongodb",
    "kafka",
    "redis",
    "docker",
    "aws",
    "backend",
    "distributed",
    "sql",
    "hibernate",
    "jpa",
]
NEGATIVE = [
    "frontend",
    "front-end",
    "front end",
    "react developer",
    "angular developer",
    "ui developer",
    "qa engineer",
    "manual testing",
    "test automation",
    "customer support",
    "technical support",
    "sales",
    "business development",
    "intern",
    "fresher",
    "trainee",
]
NEGATIVE_BODY = [
    "frontend developer",
    "front-end developer",
    "react developer",
    "angular developer",
    "ui developer",
    "qa engineer",
    "manual testing",
    "test automation",
    "customer support",
    "technical support",
    "business development",
    "intern",
    "fresher",
    "trainee",
]


def _salary_ok(salary_text: str | None, min_lpa: float) -> str:
    if not salary_text or not min_lpa:
        return "unknown"
    text = salary_text.lower().replace(",", "")
    values: list[float] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)", text):
        num = float(match.group(1))
        if "lpa" in text or "lakh" in text or "lac" in text:
            values.append(num)
        # Raw rupee figures are usually annual.
        elif ("₹" in salary_text or "inr" in text) and num > 100000:
            values.append(num / 100000)
    if not values:
        return "unknown"
    high = max(values)
    return "yes" if high >= min_lpa else "no"


def _years_required(text: str) -> int | None:
    patterns = [
        r"(\d{1,2})\s*(?:\+|plus)?\s*(?:to|-)\s*(\d{1,2})\s*(?:\+)?\s*years?",
        r"(\d{1,2})\s*(?:\+|plus)\s*years?",
        r"minimum\s+(?:of\s+)?(\d{1,2})\s*years?",
        r"at\s+least\s+(\d{1,2})\s*years?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return (
                    int(m.group(2))
                    if m.lastindex and m.lastindex >= 2
                    else int(m.group(1))
                )
            except (TypeError, ValueError):
                continue
    return None


def heuristic_score(job: dict, cfg: dict) -> dict:
    c = cfg.get("candidate", {})
    title = str(job.get("title") or "").lower()
    desc = str(job.get("description") or "").lower()
    location = str(job.get("location") or "").lower()
    combined = f"{title}\n{desc}"
    min_lpa = float(c.get("min_salary_lpa") or 0)

    score = 28
    reasons: list[str] = []
    flags: list[str] = []

    title_hits = [k for k in POSITIVE_TITLE if k in title]
    if title_hits:
        score += min(28, 14 + 7 * len(title_hits))
        reasons.append("title matches " + ", ".join(title_hits[:3]))

    body_hits = sorted({k for k in POSITIVE_BODY if k in combined})
    if body_hits:
        score += min(28, 4 * len(body_hits))
        reasons.append("description mentions " + ", ".join(body_hits[:5]))

    location_ok = [
        loc for loc in (c.get("locations_ok") or []) if str(loc).lower() in location
    ]
    if job.get("is_remote") or "remote" in location:
        score += 10
        reasons.append("remote")
    elif location_ok:
        score += 12
        reasons.append("location matches " + location_ok[0])
    elif location:
        score -= 10
        flags.append(f"location may not match: {job.get('location')}")

    salary_ok = _salary_ok(job.get("salary_text"), min_lpa)
    if salary_ok == "yes":
        score += 6
        reasons.append("salary meets minimum")
    elif salary_ok == "no":
        score -= 25
        flags.append(f"listed salary below {min_lpa:g} LPA")

    neg_hits = [k for k in NEGATIVE if k in title]
    neg_hits += [k for k in NEGATIVE_BODY if k in desc[:2500] and k not in neg_hits]
    if neg_hits:
        score -= min(35, 12 * len(neg_hits))
        flags.append("avoid terms: " + ", ".join(neg_hits[:3]))

    years = _years_required(combined)
    if years is not None and years >= 7:
        score -= 25
        flags.append(f"asks for {years}+ years")

    score = max(0, min(100, score))
    if not reasons:
        reasons.append("no strong keyword match")
    if not flags:
        flags.append("")
    return {
        "score": score,
        "reason": "Offline keyword score: " + "; ".join(reasons) + ".",
        "red_flags": "; ".join(flags).strip("; "),
        "salary_ok": salary_ok,
        "score_method": "heuristic",
    }


def _llm_score(job: dict, cfg: dict, resume: dict, llm: LLM) -> dict:
    prompt = PROMPT.format(
        profile=candidate_profile_text(cfg, resume),
        title=job.get("title") or "",
        company=job.get("company") or "",
        location=job.get("location") or "",
        remote="yes" if job.get("is_remote") else "no",
        salary=job.get("salary_text") or "not stated",
        description=(job.get("description") or "")[:6000],
    )
    data = llm.generate_json(prompt)
    try:
        score = int(float(data.get("score", 0)))
    except (TypeError, ValueError):
        score = 0
    salary_ok = str(data.get("salary_ok") or "unknown").lower()
    if salary_ok not in {"yes", "no", "unknown"}:
        salary_ok = "unknown"
    red_flags = data.get("red_flags", "")
    if isinstance(red_flags, list):
        red_flags = "; ".join(map(str, red_flags))
    return {
        "score": max(0, min(100, score)),
        "reason": str(data.get("reason") or "").strip(),
        "red_flags": str(red_flags or "").strip(),
        "salary_ok": salary_ok,
        "score_method": f"llm:{llm.model}",
    }


def score_jobs(
    conn,
    cfg: dict,
    resume: dict,
    limit: int | None = None,
    rescore: bool = False,
    progress=print,
) -> int:
    limit = int(limit or cfg["scoring"].get("max_per_run", 80))
    rows = db.pending_scoring(conn, limit, rescore=rescore)
    if not rows:
        progress("No jobs waiting to be scored.")
        return 0

    llm = LLM(cfg)
    if llm.available:
        progress(f"Scoring {len(rows)} jobs with {llm.describe()}...")
    else:
        progress(
            f"Scoring {len(rows)} jobs with offline heuristics "
            "(set GEMINI_API_KEY in .env for LLM scoring)."
        )

    scored = 0
    for index, job in enumerate(rows, 1):
        result: dict | None = None
        if llm.available:
            try:
                result = _llm_score(job, cfg, resume, llm)
            except LLMError as exc:
                progress(f"[warn] LLM scoring failed for {job.get('title')!r}: {exc}")
                if "quota" in str(exc).lower() or "429" in str(exc):
                    progress("Hit the rate/quota limit. Stopping; rerun later.")
                    break
        if result is None:
            result = heuristic_score(job, cfg)

        conn.execute(
            """UPDATE jobs SET score=?, reason=?, red_flags=?, salary_ok=?,
                              score_method=?, updated_at=?
               WHERE id=?""",
            (
                result["score"],
                result["reason"],
                result["red_flags"],
                result["salary_ok"],
                result["score_method"],
                db.utcnow(),
                job["id"],
            ),
        )
        conn.commit()
        scored += 1
        progress(
            f"[{index}/{len(rows)}] {result['score']:>3}  "
            f"{(job.get('title') or '')[:60]} @ {(job.get('company') or '')[:30]}"
        )
    progress(f"Scored {scored} jobs.")
    return scored
