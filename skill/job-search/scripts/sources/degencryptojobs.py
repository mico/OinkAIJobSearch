"""Degen Crypto Jobs (degencryptojobs.com) — crypto job aggregator, public JSON.

Module key: ``degencryptojobs``

Integration choice (probed 2026-11-11):
  - The site's own front end reads ``GET https://www.degencryptojobs.com/api/jobs``
    (no auth), which returns ``{"jobs": [...], "total", "page", "totalPages"}``
    with 200 postings per page (~600 KB). Page 1 holds the newest postings,
    though a couple of older ones may be pinned at the top, so we sort by
    ``datePosted`` ourselves. ``limit`` / ``sort`` params are ignored.
  - Each posting has title, company, ``location`` (list), salary, full-text
    ``description``, tags, ``datePosted`` (ISO) and ``link`` — the original
    ATS posting (Greenhouse / Lever / Workable) — plus ``source`` (the ATS).
  - No RSS, sitemap or robots.txt (404). One request per run.

Postings are deduped by ``link`` (the feed repeats a few). No pre-filtering —
the AI scorer is the matching gate.
"""
from __future__ import annotations

import logging

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake
import forensic

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}
API_URL = "https://www.degencryptojobs.com/api/jobs"


def parse_jobs(payload: dict) -> list[Job]:
    raw = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    rows = [j for j in raw if isinstance(j, dict)]
    rows.sort(key=lambda j: str(j.get("datePosted") or ""), reverse=True)
    out: list[Job] = []
    seen: set[str] = set()
    for j in rows:
        url = (j.get("link") or "").strip()
        title = fix_mojibake((j.get("title") or "").strip())
        if not url or not title or url in seen:
            continue
        seen.add(url)
        company = fix_mojibake((j.get("company") or "").strip())
        loc = j.get("location")
        if isinstance(loc, list):
            location = ", ".join(str(x).strip() for x in loc if x)
        else:
            location = str(loc or "").strip()
        salary = (j.get("salary") or "").strip()
        tags = j.get("topTags") or j.get("tags") or []
        bits = [
            f"{title} at {company}" if company else title,
            "Tags: " + ", ".join(str(t) for t in tags)[:160] if tags else "",
            salary,
            j.get("description") or "",
        ]
        out.append(Job(
            source="degencryptojobs",
            external_id=url,
            title=title[:160],
            company=company,
            location=location[:160] or "Unknown",
            url=url,
            posted_at=str(j.get("datePosted") or ""),
            snippet=clean_snippet(" · ".join(b for b in bits if b), max_chars=400),
            salary=salary,
        ))
    return out


def fetch(filters: dict) -> list[Job]:
    cap = int(filters.get("max_per_source") or 36)
    jobs: list[Job] = []
    status_code = None
    err = None
    try:
        r = requests.get(API_URL, headers=UA, timeout=30)
        status_code = r.status_code
        r.raise_for_status()
        jobs = parse_jobs(r.json())[:cap]
    except (requests.RequestException, ValueError) as e:
        log.error("degencryptojobs fetch failed: %s", e)
        err = {"class": type(e).__name__, "message": str(e)[:300]}
    forensic.log_step(
        "degencryptojobs.fetch",
        input={"endpoint": API_URL, "max_per_source": cap},
        output={
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": [j.title[:80] for j in jobs[:3]],
        },
        error=err,
    )
    return jobs
