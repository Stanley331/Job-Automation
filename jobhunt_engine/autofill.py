"""Browser-assisted application filling.

This module fills known fields, uploads the tailored resume and pastes drafted
screening answers. It NEVER clicks a submit button. You review and submit.

Heuristics are intentionally generic because every ATS is different. The
persistent browser profile keeps you logged in between runs.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from . import answers as answers_mod
from . import db
from .config import (
    ConfigError,
    ensure_dirs,
    flat_candidate,
    load_answer_bank,
    load_config,
    load_resume,
    output_dir,
    resolve_path,
)

SCAN_JS = r"""
(start) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.display !== 'none' &&
           st.visibility !== 'hidden' && !el.disabled;
  };
  const labelFor = el => {
    if (el.id) {
      try {
        const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (l) return norm(l.innerText);
      } catch (e) {}
    }
    const wrap = el.closest('label');
    return wrap ? norm(wrap.innerText) : '';
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset');
    if (fs) {
      const lg = fs.querySelector('legend');
      if (lg) return norm(lg.innerText);
    }
    let p = el.parentElement;
    for (let i = 0; i < 4 && p; i++, p = p.parentElement) {
      const t = norm(p.innerText);
      if (t && t.length >= 4 && t.length < 260) return t;
    }
    return '';
  };
  const out = [];
  let idx = start;
  document.querySelectorAll('input, textarea, select').forEach(el => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'select' ? 'select' : tag)).toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image', 'password', 'search'].includes(type)) return;
    if (type !== 'file' && !visible(el)) return;
    const label = labelFor(el);
    const group = type === 'radio' ? groupLabel(el) : '';
    const question = norm(
      [label, el.getAttribute('aria-label'), el.getAttribute('placeholder'), group]
        .filter(Boolean).join(' ')
    ) || norm(el.getAttribute('name') || el.id || '');
    const f = {
      idx: idx,
      tag: tag,
      type: type,
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      autocomplete: el.getAttribute('autocomplete') || '',
      label: label,
      question: question,
      group: type === 'radio' ? (el.getAttribute('name') || '') : '',
      group_label: group,
      required: !!el.required,
      value: (el.value || '').slice(0, 300),
      checked: !!el.checked,
      maxlength: el.maxLength > 0 ? el.maxLength : null,
      accept: el.getAttribute('accept') || '',
      multiple: !!el.multiple
    };
    if (type === 'radio') {
      f.option_label = label || norm(el.parentElement ? el.parentElement.innerText : '');
    }
    if (tag === 'select') {
      f.options = Array.from(el.options).map(o => ({value: o.value, text: norm(o.text)}));
    }
    el.setAttribute('data-jobhunt-idx', String(idx));
    out.push(f);
    idx++;
  });
  return out;
}
"""

FILL_JS = r"""
({idx, value}) => {
  const el = document.querySelector('[data-jobhunt-idx="' + idx + '"]');
  if (!el) return {ok: false, reason: 'field not found'};
  const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const setVal = (proto, v) => {
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (!desc || !desc.set) { el.value = v; return; }
    desc.set.call(el, v);
  };
  if (el.tagName === 'SELECT') {
    const target = norm(value);
    const opts = Array.from(el.options);
    let opt = opts.find(o => norm(o.text) === target || norm(o.value) === target);
    if (!opt) {
      opt = opts.find(o => {
        const ot = norm(o.text);
        return ot && target && (ot.includes(target) || target.includes(ot));
      });
    }
    if (!opt) return {ok: false, reason: 'no matching option'};
    el.value = opt.value;
  } else if (el.tagName === 'TEXTAREA') {
    setVal(window.HTMLTextAreaElement.prototype, value);
  } else {
    setVal(window.HTMLInputElement.prototype, value);
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new Event('blur', {bubbles: true}));
  el.style.outline = '2px solid #16a34a';
  el.style.outlineOffset = '1px';
  return {ok: true};
}
"""

RADIO_JS = r"""
({idx}) => {
  const el = document.querySelector('[data-jobhunt-idx="' + idx + '"]');
  if (!el) return {ok: false};
  el.click();
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.style.outline = '2px solid #16a34a';
  el.style.outlineOffset = '1px';
  return {ok: true};
}
"""

HIGHLIGHT_JS = r"""
({idx, color}) => {
  const el = document.querySelector('[data-jobhunt-idx="' + idx + '"]');
  if (!el) return false;
  el.style.outline = '2px solid ' + color;
  el.style.outlineOffset = '1px';
  return true;
}
"""

BANNER_JS = r"""
() => {
  if (document.getElementById('jobhunt-banner')) return;
  const el = document.createElement('div');
  el.id = 'jobhunt-banner';
  el.textContent = 'JobHunt filled this form. Review every field, then click Submit yourself.';
  el.style.cssText = [
    'position:fixed', 'right:18px', 'bottom:18px', 'z-index:2147483647',
    'max-width:380px', 'padding:12px 16px', 'border-radius:10px',
    'background:#0f172a', 'color:#e2e8f0', 'font:13px/1.45 system-ui,sans-serif',
    'box-shadow:0 10px 30px rgba(0,0,0,.35)', 'border:1px solid #334155'
  ].join(';');
  document.body.appendChild(el);
}
"""

PASSWORD_JS = r"""
() => Array.from(document.querySelectorAll('input[type=password]')).some(el => {
  const r = el.getBoundingClientRect();
  const st = window.getComputedStyle(el);
  return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
})
"""

APPLY_RE = re.compile(
    r"^\s*(easy\s+)?apply(\s+(now|for this job|to this job|on company site|with .+))?\s*$",
    re.IGNORECASE,
)

SENSITIVE_RE = re.compile(
    r"\b(password|ssn|social security|aadhaar|aadhar|passport|bank|account number|"
    r"ifsc|credit card|date of birth|dob|gender|race|ethnicity|veteran|disability|"
    r"religion|marital status|caste|pan number)\b",
    re.IGNORECASE,
)

FIELD_RULES: list[tuple[str, re.Pattern]] = [
    ("first_name", re.compile(r"\b(first|given|preferred)\s*name\b", re.IGNORECASE)),
    ("last_name", re.compile(r"\b(last|family|sur)\s*name\b", re.IGNORECASE)),
    ("email", re.compile(r"\b(e[-\s]?mail)\b", re.IGNORECASE)),
    (
        "phone",
        re.compile(
            r"\b(phone|mobile|telephone|contact number|whatsapp)\b", re.IGNORECASE
        ),
    ),
    ("linkedin", re.compile(r"linked\s*in", re.IGNORECASE)),
    ("github", re.compile(r"git\s*hub", re.IGNORECASE)),
    (
        "portfolio",
        re.compile(r"\b(portfolio|personal website|website|blog)\b", re.IGNORECASE),
    ),
    (
        "current_company",
        re.compile(
            r"\b(current|present|latest)\s+(company|employer|organization|organisation)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "current_title",
        re.compile(
            r"\b(current|present|latest)\s+(title|designation|job title|role)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "years_experience",
        re.compile(
            r"\b(years?|yrs?)\s+(of\s+)?experience\b|\btotal\s+experience\b",
            re.IGNORECASE,
        ),
    ),
    (
        "notice_period",
        re.compile(
            r"\bnotice\s*period\b|\bjoining\s+time\b|\bhow\s+soon\s+can\s+you\s+join\b",
            re.IGNORECASE,
        ),
    ),
    (
        "expected_ctc",
        re.compile(
            r"\b(expected|desired)\s+(ctc|salary|compensation|pay)\b|\bsalary\s+expectation",
            re.IGNORECASE,
        ),
    ),
    (
        "current_ctc",
        re.compile(r"\b(current|present)\s+(ctc|salary|compensation)\b", re.IGNORECASE),
    ),
    (
        "work_authorization",
        re.compile(
            r"\b(work\s+authori[sz]ation|right\s+to\s+work|legally\s+(authorized|allowed))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "willing_relocate",
        re.compile(r"\b(willing|able)\s+to\s+relocate\b|\brelocation\b", re.IGNORECASE),
    ),
    (
        "city",
        re.compile(
            r"\b(city|current location|where are you based|location)\b", re.IGNORECASE
        ),
    ),
    ("country", re.compile(r"\bcountry\b", re.IGNORECASE)),
    (
        "availability",
        re.compile(r"\b(availability|available from|start date)\b", re.IGNORECASE),
    ),
]

NAME_LABELS = {
    "name",
    "full name",
    "your name",
    "applicant name",
    "candidate name",
    "full legal name",
    "legal name",
}


def _norm_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip(" *:?")


def _yes_no(value: str) -> str | None:
    first = answers_mod.normalize(value).split(" ")[0] if value else ""
    if first in {"yes", "y"}:
        return "yes"
    if first in {"no", "n"}:
        return "no"
    return None


def _has_value(field: dict) -> bool:
    value = str(field.get("value") or "").strip()
    if not value:
        return False
    lowered = value.lower()
    return lowered not in {
        "select",
        "select an option",
        "choose",
        "please select",
        "enter your answer",
        "type here",
    }


def _looks_like_cover(text: str) -> bool:
    return bool(
        re.search(
            r"cover\s*letter|message to (the )?(hiring|recruiting)|"
            r"why do you want to work",
            text,
            re.IGNORECASE,
        )
    )


def _candidate_value(field: dict, candidate: dict) -> tuple[str, str] | None:
    label = _norm_label(field.get("label") or "")
    question = _norm_label(field.get("question") or "")
    combined = " ".join(
        filter(
            None,
            [
                field.get("label"),
                field.get("placeholder"),
                field.get("name"),
                field.get("id"),
                field.get("autocomplete"),
                field.get("question"),
            ],
        )
    )
    combined = re.sub(r"\s+", " ", combined).strip()
    lowered = combined.lower()

    if field.get("type") == "email" or re.search(r"\b(e[-\s]?mail)\b", lowered):
        return candidate.get("email"), "Email"
    if field.get("type") == "tel" or re.search(
        r"\b(phone|mobile|telephone|contact number)\b", lowered
    ):
        return candidate.get("phone"), "Phone"
    if "linkedin" in lowered:
        return candidate.get("linkedin"), "LinkedIn"
    if "github" in lowered:
        return candidate.get("github"), "GitHub"

    if label in NAME_LABELS or question in NAME_LABELS:
        return candidate.get("full_name"), "Full name"

    for key, regex in FIELD_RULES:
        if regex.search(lowered):
            value = candidate.get(key)
            if value:
                return str(value), key.replace("_", " ").title()
    return None


def _answer_value(
    field: dict,
    prepared: list[dict],
    bank: list[dict],
    candidate: dict,
    cover_text: str,
) -> tuple[str, str] | None:
    question = (
        field.get("group_label")
        or field.get("question")
        or field.get("label")
        or field.get("name")
        or ""
    )
    found = answers_mod.answer_for(question, prepared, bank, candidate)
    if found:
        answer, source, _ = found
        return answer, f"screening answer ({source})"
    if field.get("tag") == "textarea" and cover_text and _looks_like_cover(question):
        return cover_text, "cover letter text"
    return None


def _value_for(
    field: dict,
    prepared: list[dict],
    bank: list[dict],
    candidate: dict,
    cover_text: str,
) -> tuple[str, str] | None:
    combined = " ".join(
        filter(
            None,
            [
                field.get("label"),
                field.get("placeholder"),
                field.get("name"),
                field.get("id"),
                field.get("question"),
            ],
        )
    )
    if SENSITIVE_RE.search(combined):
        return None

    ftype = field.get("type")
    if ftype in {"radio", "select"}:
        answer = _answer_value(field, prepared, bank, candidate, cover_text)
        if answer:
            return answer
        return _candidate_value(field, candidate)

    if field.get("tag") == "textarea":
        return _answer_value(field, prepared, bank, candidate, cover_text)

    simple = _candidate_value(field, candidate)
    if simple:
        return simple
    if "?" in str(field.get("question") or "") or ftype == "text":
        return _answer_value(field, prepared, bank, candidate, cover_text)
    return None


def _scan(page) -> list[dict]:
    from playwright.sync_api import Error as PlaywrightError

    fields: list[dict] = []
    start = 0
    for frame in page.frames:
        try:
            batch = frame.evaluate(SCAN_JS, start)
        except PlaywrightError:
            continue
        for field in batch or []:
            field["_frame"] = frame
            fields.append(field)
        start += len(batch or [])
    return fields


def _has_password(page) -> bool:
    from playwright.sync_api import Error as PlaywrightError

    for frame in page.frames:
        try:
            if frame.evaluate(PASSWORD_JS):
                return True
        except PlaywrightError:
            continue
    return False


def _click_apply(page, progress: Callable[[str], None]):
    from playwright.sync_api import Error as PlaywrightError

    new_pages: list = []
    page.context.on("page", lambda p: new_pages.append(p))
    for role in ("button", "link"):
        try:
            locator = page.get_by_role(role, name=APPLY_RE)
            count = min(locator.count(), 8)
        except PlaywrightError:
            continue
        for i in range(count):
            el = locator.nth(i)
            try:
                if not el.is_visible():
                    continue
                text = (el.inner_text() or "").strip()
                progress(f"Clicking {text!r}...")
                el.click(timeout=5000)
                page.wait_for_timeout(2500)
                if new_pages:
                    return new_pages[-1]
                return page
            except PlaywrightError:
                continue
    progress("No Apply button detected; continuing on the current page.")
    return page


def _wait_for_form(page, progress, seconds: int = 15) -> list[dict]:
    for _ in range(max(1, seconds)):
        fields = _scan(page)
        if fields:
            return fields
        page.wait_for_timeout(1000)
    progress("No form fields detected yet. The browser stays open so you can navigate.")
    return []


def _print_summary(
    progress: Callable[[str], None],
    filled: list[str],
    unanswered: list[tuple[str, bool]],
    skipped: int,
    uploaded: list[str],
) -> None:
    progress("")
    if filled:
        progress(f"Filled {len(filled)} field(s):")
        for item in filled[:25]:
            progress(f"  + {item}")
        if len(filled) > 25:
            progress(f"  ... and {len(filled) - 25} more")
    if uploaded:
        progress("Uploaded:")
        for item in uploaded:
            progress(f"  + {item}")
    if unanswered:
        progress(f"Needs your attention ({len(unanswered)}):")
        for question, required in unanswered[:20]:
            flag = "required" if required else "optional"
            progress(f"  ! {question[:110]} ({flag})")
    if skipped:
        progress(
            f"Skipped {skipped} field(s) that were already filled, sensitive, or unrecognised."
        )
    progress("")
    progress("No submit button was clicked. Review the form and submit it yourself.")


def _fill_fields(
    page,
    fields: list[dict],
    prepared: list[dict],
    bank: list[dict],
    candidate: dict,
    cover_text: str,
    resume_path: Path | None,
    cover_path: Path | None,
    cfg: dict,
    progress: Callable[[str], None],
) -> dict:
    from playwright.sync_api import Error as PlaywrightError

    filled: list[str] = []
    unanswered: list[tuple[str, bool]] = []
    uploaded: list[str] = []
    skipped = 0
    auto_consent = bool(cfg["autofill"].get("auto_check_consent"))

    # Group radios so one answer selects one option.
    radio_groups: dict[tuple, list[dict]] = {}
    for field in fields:
        if field.get("type") == "radio":
            key = (id(field["_frame"]), field.get("group") or field.get("question"))
            radio_groups.setdefault(key, []).append(field)
    handled_radios: set[int] = set()

    for field in fields:
        frame = field["_frame"]
        idx = field["idx"]
        ftype = field.get("type")

        if ftype == "radio":
            key = (id(frame), field.get("group") or field.get("question"))
            if id(field) in handled_radios:
                continue
            group = radio_groups.get(key, [field])
            handled_radios.update(id(item) for item in group)
            if any(item.get("checked") for item in group):
                skipped += 1
                continue
            question = group[0].get("group_label") or group[0].get("question") or ""
            answer = _answer_value(group[0], prepared, bank, candidate, cover_text)
            if not answer:
                unanswered.append(
                    (question, any(item.get("required") for item in group))
                )
                continue
            value = answer[0]
            yn = _yes_no(value)
            target = None
            target_value = answers_mod.normalize(value)
            for item in group:
                option = answers_mod.normalize(item.get("option_label") or "")
                if yn and option == yn:
                    target = item
                    break
                if option and (
                    option == target_value
                    or option in target_value
                    or target_value in option
                ):
                    target = item
                    break
            if not target:
                unanswered.append(
                    (question, any(item.get("required") for item in group))
                )
                continue
            try:
                result = frame.evaluate(RADIO_JS, {"idx": target["idx"]})
                if result.get("ok"):
                    filled.append(f"{question[:80]} -> {value[:60]}")
                else:
                    unanswered.append(
                        (question, any(item.get("required") for item in group))
                    )
            except PlaywrightError:
                unanswered.append(
                    (question, any(item.get("required") for item in group))
                )
            continue

        if ftype == "checkbox":
            label = f"{field.get('label')} {field.get('name')}".lower()
            if auto_consent and re.search(
                r"\b(agree|consent|privacy|terms|acknowledge)\b", label
            ):
                try:
                    if not field.get("checked"):
                        frame.evaluate(RADIO_JS, {"idx": idx})
                        filled.append(f"Checked consent: {field.get('label')[:70]}")
                except PlaywrightError:
                    pass
            else:
                skipped += 1
            continue

        if ftype == "file":
            label = (
                f"{field.get('label')} {field.get('name')} {field.get('id')}".lower()
            )
            path: Path | None = None
            if "cover" in label:
                path = cover_path
            elif (
                re.search(r"\b(resume|cv|curriculum)\b", label)
                or "pdf" in (field.get("accept") or "").lower()
                or "doc" in (field.get("accept") or "").lower()
            ):
                path = resume_path
            if not path or not path.exists():
                unanswered.append(
                    (
                        field.get("label") or field.get("name") or "File upload",
                        bool(field.get("required")),
                    )
                )
                continue
            try:
                frame.locator(f'[data-jobhunt-idx="{idx}"]').set_input_files(str(path))
                uploaded.append(
                    f"{path.name} -> {field.get('label') or field.get('name') or 'file input'}"
                )
            except PlaywrightError as exc:
                unanswered.append(
                    (
                        f"{field.get('label') or 'File upload'} ({exc})",
                        bool(field.get("required")),
                    )
                )
            continue

        if _has_value(field):
            skipped += 1
            continue

        resolved = _value_for(field, prepared, bank, candidate, cover_text)
        if not resolved:
            question = (
                field.get("question") or field.get("label") or field.get("name") or ""
            )
            if field.get("required") or field.get("tag") == "textarea":
                unanswered.append((question, bool(field.get("required"))))
            else:
                skipped += 1
            continue
        value = resolved[0]
        try:
            result = frame.evaluate(FILL_JS, {"idx": idx, "value": str(value)})
        except PlaywrightError:
            result = {"ok": False, "reason": "browser error"}
        if result.get("ok"):
            label = (
                field.get("label")
                or field.get("question")
                or field.get("name")
                or "field"
            )
            filled.append(f"{label[:80]} -> {str(value)[:80]}")
        else:
            question = (
                field.get("question") or field.get("label") or field.get("name") or ""
            )
            unanswered.append(
                (f"{question} [{result.get('reason')}]", bool(field.get("required")))
            )

    return {
        "filled": filled,
        "unanswered": unanswered,
        "uploaded": uploaded,
        "skipped": skipped,
    }


def run_autofill(
    job_ref: str | int,
    cfg: dict | None = None,
    dry_run: bool = False,
    click_apply: bool | None = None,
    headless: bool | None = None,
    keep_open: bool = True,
    prepare_if_missing: bool = False,
    progress: Callable[[str], None] = print,
) -> dict:
    cfg = cfg or load_config()
    ensure_dirs(cfg)
    conn = db.connect(cfg)
    try:
        job = db.resolve_job(conn, job_ref)
        if not job:
            raise ConfigError(f"No job found matching {job_ref!r}.")
        job_id = int(job["id"])
        app = db.get_application(conn, job_id)
        resume_rel = (app or {}).get("resume_pdf") or (app or {}).get("resume_docx")
        if not resume_rel and prepare_if_missing:
            conn.close()
            from .prepare import prepare_job

            prepare_job(job_id, cfg=cfg, progress=progress)
            conn = db.connect(cfg)
            app = db.get_application(conn, job_id)
            resume_rel = (app or {}).get("resume_pdf") or (app or {}).get("resume_docx")
        if not resume_rel:
            raise ConfigError(
                "No tailored resume found. Run `python jobhunt.py prepare "
                f"{job_id}` first, or add --prepare."
            )

        out = output_dir(cfg)
        resume_path = out / resume_rel
        if not resume_path.exists():
            raise ConfigError(f"Resume file is missing: {resume_path}. Re-run prepare.")
        cover_rel = (app or {}).get("cover_letter_pdf")
        cover_path = out / cover_rel if cover_rel else None
        if cover_path and not cover_path.exists():
            cover_path = None

        prepared = (app or {}).get("answers") or []
        bank = load_answer_bank()
        master = load_resume()
        candidate = flat_candidate(cfg, master)
        cover_text = str((app or {}).get("meta", {}).get("cover_letter_text") or "")

        target = job.get("apply_url") or job.get("url")
        if not target:
            raise ConfigError("This job has no URL.")
        click = (
            cfg["autofill"].get("click_apply", True)
            if click_apply is None
            else click_apply
        )
        is_headless = (
            cfg["autofill"].get("headless", False) if headless is None else headless
        )
        profile_dir = resolve_path(cfg, cfg["autofill"]["browser_profile"])
        profile_dir.mkdir(parents=True, exist_ok=True)

        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        progress(f"Opening: {target}")
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=bool(is_headless),
                slow_mo=int(cfg["autofill"].get("slow_mo_ms", 40)),
                viewport={"width": 1440, "height": 960},
                accept_downloads=True,
                locale="en-IN",
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                try:
                    page.goto(target, wait_until="domcontentloaded", timeout=60000)
                except PlaywrightError as exc:
                    progress(f"[warn] page load issue: {exc}")
                page.wait_for_timeout(2500)

                if click:
                    page = _click_apply(page, progress)
                    page.wait_for_timeout(1500)

                fields = _wait_for_form(page, progress)
                login_like = (
                    _has_password(page)
                    and not any(f.get("type") == "file" for f in fields)
                    and len(fields) < 5
                )
                if login_like:
                    progress(
                        "This looks like a login/account page. Log in in the browser, "
                        "then close it and rerun `apply`. No fields were filled."
                    )
                    fields = []

                if dry_run:
                    progress("")
                    progress(
                        f"Detected {len(fields)} field(s) (dry run - nothing filled):"
                    )
                    for field in fields:
                        resolved = _value_for(
                            field, prepared, bank, candidate, cover_text
                        )
                        shown = resolved[0] if resolved else "(no match)"
                        label = (
                            field.get("label")
                            or field.get("question")
                            or field.get("name")
                            or "field"
                        )
                        progress(
                            f"  [{field.get('type')}] {label[:80]} -> {str(shown)[:80]}"
                        )
                    summary = {
                        "dry_run": True,
                        "fields": len(fields),
                        "filled": [],
                        "unanswered": [],
                        "uploaded": [],
                        "skipped": 0,
                    }
                    db.log_event(conn, job_id, "autofill_dry_run", json.dumps(summary))
                    return summary

                result = _fill_fields(
                    page,
                    fields,
                    prepared,
                    bank,
                    candidate,
                    cover_text,
                    resume_path,
                    cover_path,
                    cfg,
                    progress,
                )
                try:
                    page.evaluate(BANNER_JS)
                except PlaywrightError:
                    pass
                _print_summary(
                    progress,
                    result["filled"],
                    result["unanswered"],
                    result["skipped"],
                    result["uploaded"],
                )
                summary = {
                    "fields": len(fields),
                    "filled": len(result["filled"]),
                    "unanswered": [q for q, _ in result["unanswered"]],
                    "uploaded": result["uploaded"],
                    "skipped": result["skipped"],
                    "url": page.url,
                }
                db.log_event(
                    conn, job_id, "autofill", json.dumps(summary, ensure_ascii=False)
                )

                if keep_open and not is_headless:
                    progress(
                        "Browser left open. Close it when you are done reviewing/submitting."
                    )
                    try:
                        while True:
                            pages = context.pages
                            if not pages or all(pg.is_closed() for pg in pages):
                                break
                            page.wait_for_timeout(1000)
                    except PlaywrightError:
                        pass
                return summary
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
    finally:
        conn.close()
