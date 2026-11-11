"""web3.career — the largest web3 / crypto job board. HTML scrape of the listing.

Module key: ``web3_career``

Integration choice (probed 2026-11-11):
  - No RSS (``/rss``, ``/feed`` → 404).
  - The official "Web3 Jobs API" is a token-gated third-party endpoint
    (sign-up by email, docs hosted on docs.bondex.app) — not usable without
    an account, so it is not wired here.
  - The server-rendered listing is clean and stable: each posting is a
    ``<tr data-jobid=N class="... table_row">`` row with the title in ``<h2>``,
    the company in ``<h3>``, a ``📍`` location span, a ``<time datetime=…>``
    publish timestamp, an optional salary ``<p>`` in ``td.cell-salary``, and
    tag badges in ``td.cell-tags``. ~15 rows per page, ``?page=N`` paginates.

robots.txt only disallows ``/metrics*``. We fetch ``web3_career_pages``
listing pages (default 3 → ~45 newest postings) sequentially with a short
pause, and do not open detail pages. No pre-filtering: the AI scorer is the
only matching gate.
"""
from __future__ import annotations

import logging
import time

import requests
from bs4 import BeautifulSoup

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake
import forensic

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}
BASE = "https://web3.career"
DEFAULT_PAGES = 3
PAGE_PAUSE_S = 1.0


def _page_url(page: int) -> str:
    return f"{BASE}/" if page <= 1 else f"{BASE}/?page={page}"


def _txt(node) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node is not None else ""


def parse_listing(html: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[Job] = []
    for row in soup.select("tr.table_row[data-jobid]"):
        job_id = (row.get("data-jobid") or "").strip()
        link = row.select_one("a[href]")
        title = fix_mojibake(_txt(row.find("h2")))
        if not job_id or link is None or not title:
            continue
        href = link.get("href", "")
        url = href if href.startswith("http") else BASE + href
        company = fix_mojibake(_txt(row.find("h3")))
        loc_node = row.select_one(".job-location-mobile")
        location = _txt(loc_node).replace("📍", "").replace(" ,", ",").strip()
        time_node = row.find("time")
        posted_at = (time_node.get("datetime") or "").strip() if time_node else ""
        salary = _txt(row.select_one("td.cell-salary"))
        tags = [_txt(a) for a in row.select("td.cell-tags a")]
        snippet_bits = [b for b in (
            f"{title} at {company}" if company else title,
            location,
            salary,
            ("Tags: " + ", ".join(t for t in tags if t)) if tags else "",
        ) if b]
        out.append(Job(
            source="web3_career",
            external_id=job_id,
            title=title[:160],
            company=company,
            location=location or "Unknown",
            url=url,
            posted_at=posted_at,
            snippet=clean_snippet(" · ".join(snippet_bits), max_chars=400),
            salary=salary,
        ))
    return out


def fetch(filters: dict) -> list[Job]:
    cap = int(filters.get("max_per_source") or 36)
    pages = max(1, int(filters.get("web3_career_pages") or DEFAULT_PAGES))
    jobs: list[Job] = []
    seen: set[str] = set()
    per_page: list[dict] = []
    for page in range(1, pages + 1):
        if len(jobs) >= cap:
            break
        url = _page_url(page)
        status_code = None
        err = None
        count = 0
        try:
            r = requests.get(url, headers=UA, timeout=25)
            status_code = r.status_code
            r.raise_for_status()
            for job in parse_listing(r.text):
                if job.external_id in seen:
                    continue  # featured rows repeat across pages
                seen.add(job.external_id)
                jobs.append(job)
                count += 1
        except requests.RequestException as e:
            log.error("web3_career page %d fetch failed: %s", page, e)
            err = {"class": type(e).__name__, "message": str(e)[:200]}
        per_page.append({"url": url, "status_code": status_code, "count": count, "error": err})
        if err is not None or count == 0:
            break
        if page < pages:
            time.sleep(PAGE_PAUSE_S)
    jobs = jobs[:cap]
    forensic.log_step(
        "web3_career.fetch",
        input={"pages": pages, "max_per_source": cap},
        output={
            "count": len(jobs),
            "per_page": per_page,
            "sample_titles": [j.title[:80] for j in jobs[:3]],
        },
    )
    return jobs
