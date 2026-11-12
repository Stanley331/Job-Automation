"""Screening-question matching and drafting.

The answer bank in data/answers.yaml is the source of truth for recurring
logistics questions. The LLM only drafts company/role-specific answers from
facts already present in the master resume.
"""

from __future__ import annotations

import html
import json
import re
from difflib import SequenceMatcher

from jinja2 import Template

from .config import candidate_profile_text
from .llm import LLM, LLMError

STOPWORDS = {
    "what",
    "is",
    "your",
    "you",
    "the",
    "a",
    "an",
    "do",
    "does",
    "are",
    "for",
    "of",
    "to",
    "in",
    "on",
    "with",
    "please",
    "tell",
    "us",
    "me",
    "about",
    "how",
    "why",
    "this",
    "that",
    "role",
    "job",
    "position",
    "company",
    "would",
    "could",
    "can",
    "have",
    "has",
    "any",
    "if",
    "at",
    "and",
    "or",
    "we",
    "our",
    "it",
    "be",
    "as",
    "by",
    "from",
    "will",
    "much",
    "many",
    "describe",
    "share",
    "provide",
}

COMMON_QUESTIONS = [
    "Why are you interested in this role at {company}?",
    "What relevant experience do you have for this role?",
    "What are your salary expectations?",
    "What is your notice period and when can you start?",
    "Are you willing to relocate?",
    "Are you authorized to work in this location?",
    "Do you require visa sponsorship?",
    "Tell us about a challenging project you worked on.",
]

DRAFT_PROMPT = """You are helping a candidate prepare screening answers for a job application.

CANDIDATE PROFILE:
{profile}

MASTER RESUME (facts only - never invent anything not present here):
{resume}

JOB POSTING:
Title: {title}
Company: {company}
Location: {location}
Description:
{description}

CANONICAL ANSWERS (use these verbatim where the question matches):
{canonical}

Write concise, first-person answers (1-4 sentences each) for the questions
below. Also include any screening questions explicitly asked in the job
posting. Ground every claim in the master resume. Never invent employers,
titles, dates, degrees, certifications, metrics or skills. If the posting
asks for information that is not in the candidate's facts, leave the answer
empty and set needs_review to true.

Questions:
{questions}

Return ONLY valid JSON:
{{"answers": [{{"question": "...", "answer": "...", "needs_review": false}}]}}
"""


def normalize(text: str) -> str:
    t = html.unescape(str(text or "")).lower()
    t = t.replace("\u2019", "'").replace("\u2013", "-").replace("\u2014", "-")
    t = re.sub(r"[\u200b-\u200f]", "", t)
    t = re.sub(r"[^a-z0-9+#.\- ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9+#.]{2,}", normalize(text)) if w not in STOPWORDS
    }


def similarity(question: str, pattern: str) -> float:
    q, p = normalize(question), normalize(pattern)
    if not q or not p:
        return 0.0
    if q == p:
        return 1.0
    if len(p) >= 4 and (p in q or q in p):
        return 0.93
    qt, pt = _tokens(q), _tokens(p)
    if qt and pt:
        jaccard = len(qt & pt) / len(qt | pt)
    else:
        jaccard = 0.0
    ratio = SequenceMatcher(None, q, p).ratio()
    return max(jaccard, ratio * 0.9)


def render_answer(entry: dict, candidate: dict) -> str:
    raw = str(entry.get("answer") or "")
    if not raw:
        return ""
    try:
        return Template(raw).render(candidate=candidate, **candidate).strip()
    except Exception:  # noqa: BLE001 - a malformed template should not crash
        return raw.strip()


def is_placeholder(answer: str) -> bool:
    s = (answer or "").strip()
    return not s or bool(
        re.search(r"\bTODO\b|\[insert|your\.email@example\.com", s, re.IGNORECASE)
    )


def match_entry(
    question: str, entries: list[dict], threshold: float = 0.62
) -> tuple[dict | None, float]:
    best: dict | None = None
    best_score = 0.0
    for entry in entries:
        patterns = entry.get("patterns") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        for pattern in patterns:
            score = similarity(question, pattern)
            if score > best_score:
                best, best_score = entry, score
    if best_score >= threshold:
        return best, best_score
    return None, best_score


def answer_for(
    question: str,
    prepared: list[dict] | None,
    bank: list[dict] | None,
    candidate: dict,
    threshold: float = 0.62,
) -> tuple[str, str, float] | None:
    """Return (answer, source, score) for a form question, or None."""
    prepared = prepared or []
    bank = bank or []
    best: tuple[str, str, float] | None = None
    best_score = 0.0
    for item in prepared:
        if not isinstance(item, dict):
            continue
        q = item.get("question") or ""
        ans = str(item.get("answer") or "").strip()
        if not ans or is_placeholder(ans):
            continue
        score = similarity(question, q)
        if score > best_score:
            best, best_score = (ans, "prepared", score), score
    if best and best_score >= threshold:
        return best

    entry, score = match_entry(question, bank, threshold=threshold)
    ans = render_answer(entry, candidate) if entry else ""
    if entry and ans and not is_placeholder(ans) and score > best_score:
        return ans, "answer_bank", score
    return best if best and best_score >= threshold else None


def _canonical_for_prompt(
    job: dict, bank: list[dict], candidate: dict
) -> tuple[str, list[dict]]:
    lines: list[str] = []
    matched: list[dict] = []
    for entry in bank:
        q = entry.get("question") or (entry.get("patterns") or [""])[0]
        ans = render_answer(entry, candidate)
        if not ans or is_placeholder(ans):
            continue
        if entry.get("category") == "demographic":
            continue
        lines.append(f"- {q}: {ans}")
        matched.append(
            {
                "question": str(q),
                "answer": ans,
                "source": "answer_bank",
                "needs_review": bool(entry.get("needs_review")),
            }
        )
    return ("\n".join(lines) if lines else "(none)"), matched


def _offline_answers(
    job: dict,
    cfg: dict,
    bank: list[dict],
    candidate: dict,
    resume: dict,
) -> list[dict]:
    out: list[dict] = []
    company = job.get("company") or "the company"
    title = job.get("title") or "this role"
    for template in COMMON_QUESTIONS:
        question = template.format(company=company, title=title)
        found = answer_for(question, [], bank, candidate, threshold=0.55)
        if found:
            answer, source, _ = found
            out.append(
                {
                    "question": question,
                    "answer": answer,
                    "source": source,
                    "needs_review": False,
                }
            )
        elif "interested in this role" in question.lower():
            out.append(
                {
                    "question": question,
                    "answer": (
                        f"I am interested in the {title} role at {company} because it "
                        "matches my experience building Java/Spring Boot backend services "
                        "and I want to work on larger-scale systems with end-to-end ownership."
                    ),
                    "source": "offline_template",
                    "needs_review": True,
                }
            )
        elif "relevant experience" in question.lower():
            summary = str(resume.get("summary") or "").strip()
            out.append(
                {
                    "question": question,
                    "answer": summary,
                    "source": "resume_summary",
                    "needs_review": not bool(summary),
                }
            )
        elif "challenging project" in question.lower():
            bullet = ""
            for project in resume.get("projects") or []:
                bullets = project.get("bullets") or []
                if bullets:
                    bullet = str(bullets[0])
                    break
            if not bullet:
                for exp in resume.get("experience") or []:
                    bullets = exp.get("bullets") or []
                    if bullets:
                        bullet = str(bullets[0])
                        break
            out.append(
                {
                    "question": question,
                    "answer": bullet,
                    "source": "resume_bullet",
                    "needs_review": True,
                }
            )
    return out


def _merge_answers(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            if not question:
                continue
            key = normalize(question)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
    return merged[:14]


def draft_answers(
    job: dict,
    resume: dict,
    cfg: dict,
    llm: LLM,
    bank: list[dict],
    candidate: dict,
) -> list[dict]:
    canonical_text, canonical = _canonical_for_prompt(job, bank, candidate)
    company = job.get("company") or "the company"
    questions = "\n".join(
        f"- {q.format(company=company, title=job.get('title') or 'this role')}"
        for q in COMMON_QUESTIONS
    )

    if not llm.available:
        return _merge_answers(
            _offline_answers(job, cfg, bank, candidate, resume), canonical
        )

    prompt = DRAFT_PROMPT.format(
        profile=candidate_profile_text(cfg, resume),
        resume=json.dumps(resume, ensure_ascii=False)[:9000],
        title=job.get("title") or "",
        company=company,
        location=job.get("location") or "",
        description=(job.get("description") or "")[:6000],
        canonical=canonical_text,
        questions=questions,
    )
    try:
        data = llm.generate_json(prompt)
    except LLMError:
        return _merge_answers(
            _offline_answers(job, cfg, bank, candidate, resume), canonical
        )

    answers: list[dict] = []
    seen: set[str] = set()

    # Canonical answers win for recurring logistics questions.
    for item in canonical:
        key = normalize(item["question"])
        if key not in seen:
            answers.append(item)
            seen.add(key)

    for item in data.get("answers", []) or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question:
            continue
        key = normalize(question)
        if key in seen:
            continue
        # If the bank knows this question, use the canonical answer.
        found = answer_for(question, [], bank, candidate, threshold=0.7)
        if found:
            answer, source, _ = found
            answers.append(
                {
                    "question": question,
                    "answer": answer,
                    "source": source,
                    "needs_review": False,
                }
            )
        else:
            answers.append(
                {
                    "question": question,
                    "answer": answer,
                    "source": "llm",
                    "needs_review": bool(item.get("needs_review"))
                    or is_placeholder(answer),
                }
            )
        seen.add(key)

    return _merge_answers(answers)
