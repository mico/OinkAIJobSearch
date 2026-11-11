"""Cryptocurrency Jobs (cryptocurrencyjobs.co) — curated crypto board, public RSS.

Module key: ``cryptocurrencyjobs``

Feed: ``https://cryptocurrencyjobs.co/index.xml`` (Hugo site feed, ~60 newest
postings, newest first). robots.txt blocks named AI-training crawlers but not
``User-agent: *``; we fetch only the feed, once per run.

Item titles are ``"<Role> at <Company>"``; the description is one sentence
naming the employment type and where the role can be based, which we keep as
the snippet so the AI scorer sees location/remote info. No pre-filtering.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake
import forensic

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}
FEED_URL = "https://cryptocurrencyjobs.co/index.xml"


def _text(item, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def _split_title(raw: str) -> tuple[str, str]:
    """``"Senior Engineer at Acme Labs"`` → (``"Senior Engineer"``, ``"Acme Labs"``).

    Splits on the LAST `` at `` so roles like "Head of Growth at Scale at X"
    keep their full title. No separator → whole string is the title.
    """
    role, sep, company = raw.rpartition(" at ")
    if not sep or not role.strip():
        return raw.strip(), ""
    return role.strip(), company.strip()


def _location_from_description(desc: str) -> str:
    """Pull "…can be based in X." out of the one-line description, else ''."""
    marker = "based in "
    i = desc.find(marker)
    if i < 0:
        return "Remote" if "remote" in desc.lower() else ""
    return desc[i + len(marker):].rstrip(" .")[:120]


def parse_feed(xml_bytes: bytes) -> list[Job]:
    root = ET.fromstring(xml_bytes)
    out: list[Job] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        url = _text(item, "link") or _text(item, "guid")
        raw_title = fix_mojibake(_text(item, "title"))
        if not url or not raw_title or url in seen:
            continue
        seen.add(url)
        title, company = _split_title(raw_title)
        desc = fix_mojibake(_text(item, "description"))
        out.append(Job(
            source="cryptocurrencyjobs",
            external_id=url,
            title=title[:160],
            company=company,
            location=_location_from_description(desc) or "Unknown",
            url=url,
            posted_at=_text(item, "pubDate"),
            snippet=clean_snippet(desc, max_chars=400),
        ))
    return out


def fetch(filters: dict) -> list[Job]:
    cap = int(filters.get("max_per_source") or 36)
    jobs: list[Job] = []
    status_code = None
    err = None
    try:
        r = requests.get(FEED_URL, headers=UA, timeout=25)
        status_code = r.status_code
        r.raise_for_status()
        jobs = parse_feed(r.content)[:cap]
    except (requests.RequestException, ET.ParseError) as e:
        log.error("cryptocurrencyjobs fetch failed: %s", e)
        err = {"class": type(e).__name__, "message": str(e)[:300]}
    forensic.log_step(
        "cryptocurrencyjobs.fetch",
        input={"endpoint": FEED_URL, "max_per_source": cap},
        output={
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": [j.title[:80] for j in jobs[:3]],
        },
        error=err,
    )
    return jobs
