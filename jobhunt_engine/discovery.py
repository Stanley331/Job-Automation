"""Job discovery through python-jobspy (Naukri, Indeed, optionally LinkedIn)."""

from __future__ import annotations

import time
from typing import Any

import pandas as pd

from . import db


def _clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _salary_text(row: pd.Series) -> str | None:
    lo = _clean(row.get("min_amount"))
    hi = _clean(row.get("max_amount"))
    if lo is None and hi is None:
        return None
    cur = _clean(row.get("currency")) or ""
    interval = _clean(row.get("interval")) or ""
    return f"{lo}-{hi} {cur} {interval}".strip()


def _normalise_row(row: pd.Series, search_term: str) -> dict | None:
    url = _clean(row.get("job_url"))
    if not url:
        return None
    apply_url = _clean(row.get("job_url_direct")) or url
    remote = _clean(row.get("is_remote"))
    description = _clean(row.get("description"))
    if description is not None:
        description = str(description)[:30000]
    return {
        "url": str(url).strip(),
        "apply_url": str(apply_url).strip(),
        "title": _clean(row.get("title")),
        "company": _clean(row.get("company")),
        "location": _clean(row.get("location")),
        "site": _clean(row.get("site")),
        "is_remote": bool(remote) if remote is not None else False,
        "date_posted": str(_clean(row.get("date_posted")) or "") or None,
        "salary_text": _salary_text(row),
        "description": description,
        "search_term": search_term,
    }


def discover(conn, cfg: dict) -> int:
    """Run every configured search and insert unseen jobs. Returns new count."""
    from jobspy import scrape_jobs

    search = cfg["search"]
    terms = search.get("terms") or []
    locations = search.get("locations") or []
    sites = search.get("sites") or []
    results = int(search.get("results_per_search", 30))
    hours_old = int(search.get("hours_old", 72))
    country = search.get("country_indeed") or "India"
    pause = float(search.get("sleep_between_searches", 3))

    if not terms or not locations or not sites:
        raise ValueError(
            "search.terms, search.locations and search.sites must be set in config.yaml"
        )

    total_new = 0
    total_seen = 0
    for term in terms:
        for loc in locations:
            location = loc.get("location") if isinstance(loc, dict) else str(loc)
            remote = bool(loc.get("remote", False)) if isinstance(loc, dict) else False
            try:
                df = scrape_jobs(
                    site_name=sites,
                    search_term=term,
                    location=location,
                    is_remote=remote,
                    results_wanted=results,
                    hours_old=hours_old,
                    country_indeed=country,
                    description_format="markdown",
                )
            except Exception as exc:  # noqa: BLE001 - one board failing is expected
                print(f"[warn] search failed: {term!r} / {location}: {exc}")
                continue

            new_for_query = 0
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    job = _normalise_row(row, term)
                    if not job:
                        continue
                    _, inserted = db.upsert_job(conn, job)
                    total_seen += 1
                    if inserted:
                        total_new += 1
                        new_for_query += 1
            print(
                f"[search] {term!r} / {location}"
                f"{' (remote)' if remote else ''}: "
                f"{len(df) if df is not None else 0} results, {new_for_query} new"
            )
            time.sleep(pause)

    print(f"Discovery done. {total_new} new jobs added ({total_seen} listings seen).")
    return total_new
