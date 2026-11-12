# JobHunt

A local, human-in-the-loop job application pipeline for Windows/macOS/Linux.

It searches job boards, scores fit, writes a tailored resume + cover letter, drafts screening answers, and opens a browser pre-filled with your details. **You review and click Submit yourself.** Everything is tracked in a local SQLite database and a web dashboard.

```
discover -> dedupe -> score -> shortlist -> tailor resume
        -> draft answers -> browser autofill -> YOU submit -> track status
```

## Safety rules baked in

- **No auto-submit.** The autofill code never clicks a submit button. A banner reminds you to review.
- **No invented experience.** Tailoring only selects/rephrases content from `data/master_resume.yaml`. Unknown skills are dropped; new numbers/metrics are rejected.
- **Template guard.** Preparation is blocked until the placeholder name/email/employers are replaced.
- **Sensitive fields are skipped** (passwords, IDs, bank details, demographics, date of birth, etc.).
- **Consent checkboxes are left unchecked** unless you explicitly enable `autofill.auto_check_consent`.
- **Local data.** The database, resumes, browser login profile and answer bank stay on your machine. Job descriptions are sent to the LLM provider you configure.

Job boards' terms of service may restrict automation. Use this for your own applications, keep the human review step, and don't run it at abusive rates.

---

## 1. Setup

Requires **Python 3.10+**.

```powershell
cd "D:\Personal Projects\Job Automation"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

On macOS/Linux use `source .venv/bin/activate`.

### Create your local config files

```powershell
Copy-Item config.example.yaml config.yaml
Copy-Item data/master_resume.example.yaml data/master_resume.yaml
```

On macOS/Linux:

```bash
cp config.example.yaml config.yaml
cp data/master_resume.example.yaml data/master_resume.yaml
```

`config.yaml` and `data/master_resume.yaml` are gitignored. Edit those local copies; the example files are safe to commit.

### Add an LLM key

Copy `.env.example` to `.env` and fill in one key:

```dotenv
GEMINI_API_KEY=your_key_here
```

Get a free Gemini key at <https://aistudio.google.com/apikey>. Any OpenAI-compatible endpoint works too:

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-4o-mini
```

Without a key the pipeline still runs using offline keyword scoring, template tailoring and the canonical answer bank. Quality is lower; the LLM is what makes the resume genuinely tailored.

### Fill in your details

1. **`config.yaml`** (copied from `config.example.yaml`) — name, email, phone, location, notice period, expected CTC, work authorization, search terms, locations, minimum salary.
2. **`data/master_resume.yaml`** (copied from `data/master_resume.example.yaml`) — your real resume. This is the single source of truth. Replace the example companies, bullets, dates and education.
3. **`data/answers.yaml`** — recurring screening answers (notice period, salary, relocation, etc.). Keep them consistent.

Or import an existing resume:

```powershell
python jobhunt.py import-resume "C:\path\to\resume.pdf"
# review data/master_resume.imported.yaml, then copy it over data/master_resume.yaml
```

### Verify

```powershell
python jobhunt.py doctor
```

Fix any `[fail]` items. `[warn]` items are usually optional (for example, missing PDF import support).

### Run the tests

```powershell
python -m pytest -q
```

The suite covers database migration and tracking, answer matching, resume-tailoring guardrails, offline scoring, and an end-to-end autofill run against `tests/fixture_form.html` (skipped automatically if Playwright Chromium is not installed).

---

## 2. Daily workflow

### Step 1 — Discover and score

```powershell
python jobhunt.py run
```

This searches every configured term/location, dedupes by URL, scores new jobs and writes `output/report.csv`.

To also prepare the top 3 matches in the same run:

```powershell
python jobhunt.py run --prepare-top 3
```

Useful variants:

```powershell
python jobhunt.py discover                 # search only
python jobhunt.py score --limit 30         # score at most 30 jobs
python jobhunt.py score --rescore          # re-score everything
python jobhunt.py list --status shortlisted
python jobhunt.py list --min-score 75
```

Scoring uses Gemini if a key is present, otherwise an offline keyword heuristic. `score_method` in the DB tells you which was used.

### Step 2 — Review in the dashboard

```powershell
python jobhunt.py dashboard
```

Opens <http://127.0.0.1:8765>. You can:

- Filter by status/score and search title, company or location.
- See the score reason, red flags and salary fit.
- Change status with the dropdown (`new -> shortlisted -> prepared -> applied -> interview -> offer / rejected / ignored`).
- Open a job and prepare the application package.

### Step 3 — Prepare a tailored application

Click **Prepare** in the dashboard, or:

```powershell
python jobhunt.py prepare 12
```

This writes:

- `output/resumes/job12_company_role.pdf` (plus `.docx` and `.html`)
- `output/cover_letters/job12_company_role.pdf` (plus `.html`)
- Drafted screening answers in the database
- A review-notes list in the dashboard

What the tailor does:

- Selects/reorders skills that already exist in your master resume.
- Picks the most relevant 3-5 bullets per role, preserving real metrics.
- Rewrites the summary for the job.
- Rejects unknown skills and any bullet containing a metric that is not in your master resume.

Always open the generated PDF and read it before using it.

### Step 4 — Autofill the application

Click **Autofill** in the dashboard, or:

```powershell
python jobhunt.py apply 12
```

What happens:

1. A Chromium window opens using a persistent profile (`.browser/profile`). Log in to Naukri/Indeed/LinkedIn once; the session is reused next time.
2. If configured, it clicks an **Apply** button on the page.
3. It scans visible fields across the page and iframes, fills name/email/phone/LinkedIn/location/current role/notice period/expected CTC, uploads your tailored resume, and pastes matching screening answers.
4. It highlights filled fields green and leaves unknown questions for you.
5. It shows a banner: review everything and click **Submit** yourself.

It never clicks Submit.

Useful options:

```powershell
python jobhunt.py apply 12 --dry-run          # list detected fields, fill nothing
python jobhunt.py apply 12 --no-click-apply   # don't click Apply; use the page as-is
python jobhunt.py apply 12 --prepare          # generate the package first if missing
python jobhunt.py apply 12 --headless         # no visible window (not recommended)
```

If a site requires login, the tool detects the login form, fills nothing, and leaves the browser open. Log in, close it, and rerun.

### Step 5 — Submit and track

After you submit in the browser, set the status to **Applied** (dashboard dropdown or CLI):

```powershell
python jobhunt.py mark 12 applied
python jobhunt.py mark 12 interview --note "Call with recruiter on Monday"
```

`applied_at` is recorded automatically the first time a job becomes `applied`.

---

## 3. Command reference

| Command | What it does |
|---|---|
| `python jobhunt.py run` | Discover, score, report |
| `python jobhunt.py run --prepare-top N` | Also prepare the top N un-prepared jobs |
| `python jobhunt.py discover` | Search configured boards only |
| `python jobhunt.py score [--limit N] [--rescore]` | Score unscored jobs |
| `python jobhunt.py list [--status S] [--min-score N] [-q TEXT]` | Terminal list |
| `python jobhunt.py prepare <id\|url>` | Tailored resume + cover letter + answers |
| `python jobhunt.py apply <id\|url>` | Browser autofill, never submits |
| `python jobhunt.py mark <id\|url> <status>` | Update tracked status |
| `python jobhunt.py dashboard [--port 8765]` | Local web UI |
| `python jobhunt.py report [--all]` | Write `output/report.csv` |
| `python jobhunt.py import-resume <file>` | Import PDF/DOCX into YAML |
| `python jobhunt.py doctor` | Environment/config checks |

Job references can be the numeric ID shown in the dashboard/list, an exact URL, a URL fragment, or a title fragment.

---

## 4. How tracking works

SQLite database: `data/jobs.db`

- `jobs` — every discovered listing, score, reason, red flags, status, first/last seen.
- `applications` — generated resume/cover paths, drafted answers, notes, applied timestamp.
- `events` — an audit trail of status changes, prepares and autofills.

Statuses:

| Status | Meaning |
|---|---|
| `new` | Discovered and scored, not reviewed |
| `shortlisted` | You want to apply |
| `prepared` | Resume/answers generated |
| `applied` | **You submitted it** |
| `interview` | In process |
| `offer` | Offer received |
| `rejected` | Closed/rejected |
| `ignored` | Not a fit / duplicate / spam |

The dashboard's **Applied** card counts applied + interview + offer; **Applied this week** uses the first `applied_at` timestamp.

---

## 5. Configuration notes

### Search

```yaml
search:
  terms: ["java developer", "spring boot developer"]
  locations:
    - { location: "Delhi", remote: false }
    - { location: "India", remote: true }
  sites: ["naukri", "indeed"]   # add "linkedin" at your own risk; it rate-limits
```

JobSpy supports several boards, but Naukri/Indeed are the most reliable for India. LinkedIn rate-limits quickly (HTTP 429).

### Scoring

`scoring.min_score_for_report` controls the CSV cutoff (default 60). `max_per_run` caps LLM calls per run. `llm.min_seconds_between_calls` throttles requests (default 6s ≈ 10/min, safe for Gemini free tier).

### Answer bank

`data/answers.yaml` entries have `patterns` (how to recognise the question) and `answer`. Placeholders like `{{ candidate.notice_period }}` are filled from `config.yaml`. Entries with `needs_review: true` are flagged in the dashboard.

Autofill matching uses exact/substring/token-similarity matching, so small wording differences are fine. Edit answers in the dashboard per job, or the YAML for recurring questions.

### Resume tailoring guardrails

- Skills not present in the master resume are dropped with a warning.
- Tailored bullets are kept only if they share enough vocabulary with a master bullet and contain no new numbers.
- All master roles are retained (no suspicious employment gaps).
- The summary falls back to the master summary if it introduces a new number.

Review warnings in the drawer before submitting.

---

## 6. Troubleshooting

**`No module named jobspy` / `playwright`**
```powershell
pip install -r requirements.txt
playwright install chromium
```

**PDF rendering failed, but HTML/DOCX exist**
Playwright Chromium is missing. Run `playwright install chromium`. Preparation still succeeds with DOCX (autofill uploads DOCX if PDF is unavailable).

**Naukri/Indeed returns 429 or no results**
The boards rate-limit. Increase `search.sleep_between_searches`, reduce `results_per_search`, or wait. Try one site at a time in `config.yaml`.

**LLM quota errors**
`score` stops and keeps the jobs unscored; rerun later. Lower `scoring.max_per_run` or raise `llm.min_seconds_between_calls`.

**Autofill filled a login form**
The tool detects visible password fields and refuses to fill when the page looks like a login. Log in in the opened browser, close it, and rerun. The persistent profile keeps you signed in.

**Autofill missed fields**
Every ATS is different. Use `--dry-run` to inspect what was detected, add a matching entry to `data/answers.yaml`, or fill the field manually. The browser stays open for this reason.

**The dashboard's Autofill button opens a console window**
It launches `python -m jobhunt_engine.cli apply <id>` in a new console so the browser and logs stay alive. Closing the console closes the browser.

**I don't have an LLM key**
Everything still runs offline, but resumes are keyword-selected rather than rewritten, cover letters use a template, and scoring is heuristic. Set `GEMINI_API_KEY` for the intended experience.

**Missing `config.yaml` or `data/master_resume.yaml`**
These are your local, gitignored files. Copy the examples and edit them:

```powershell
Copy-Item config.example.yaml config.yaml
Copy-Item data/master_resume.example.yaml data/master_resume.yaml
```

---

## 7. Project layout

```
jobhunt.py                     CLI entry point
config.example.yaml            example config (copy to config.yaml)
config.yaml                    your local config (gitignored)
data/
  master_resume.example.yaml   example resume (copy to master_resume.yaml)
  master_resume.yaml           your local source-of-truth resume (gitignored)
  answers.yaml                 canonical screening answers
  jobs.db                      SQLite tracking DB (created at runtime)
jobhunt_engine/
  config.py                    YAML loading + candidate facts
  db.py                        schema, migrations, tracking
  discovery.py                 JobSpy search
  scoring.py                   LLM + offline scoring
  llm.py                       Gemini / OpenAI-compatible client
  resume.py                    tailoring, validation, PDF/DOCX/HTML
  answers.py                   question matching + answer drafting
  prepare.py                   one-application orchestrator
  autofill.py                  Playwright form filling (no submit)
  dashboard.py                 FastAPI server
  importer.py                  PDF/DOCX -> YAML resume import
  templates/                   resume, cover letter, dashboard
tests/                         pytest suite + local autofill fixtures
output/                        generated resumes, cover letters, report.csv
.browser/profile/              persistent browser logins (gitignored)
```

---

## 8. Limitations / next steps

- Form heuristics are generic. Site-specific adapters (Workday, Greenhouse, Lever) would improve coverage; the current design handles most label-based forms and iframes.
- Naukri and Indeed change their markup and anti-bot rules regularly. Discovery quality depends on JobSpy.
- The LLM can still phrase something awkwardly. The generated PDF is a draft, not a final artifact.
- This is a single-user local tool. Do not expose the dashboard to the internet; it has no authentication.
