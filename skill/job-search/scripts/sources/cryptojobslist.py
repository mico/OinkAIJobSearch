"""CryptoJobsList (cryptojobslist.com) — crypto / web3 job board, public RSS.

Module key: ``cryptojobslist``

Feed: ``https://cryptojobslist.com/rss`` 301-redirects to
``https://api.cryptojobslist.com/jobs.rss`` (~100 newest postings). We hit the
final URL directly. robots.txt disallows ``/api/`` and apply pages only; the
``/jobs.rss`` feed path is allowed.

Per item the feed carries: ``title`` (role only), ``dc:creator`` (company),
``media:location``, ``link`` (stable job page, used as the external id) and an
HTML ``description`` with tags + the full posting body. The feed has no
``pubDate``, so ``posted_at`` stays empty — cross-run dedupe in the DB is what
stops repeats. No pre-filtering: the AI scorer is the only matching gate.
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
FEED_URL = "https://api.cryptojobslist.com/jobs.rss"

_DC = "{http://purl.org/dc/elements/1.1/}"
_MEDIA = "{http://search.yahoo.com/mrss/}"


def _text(item, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def parse_feed(xml_bytes: bytes) -> list[Job]:
    root = ET.fromstring(xml_bytes)
    out: list[Job] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        url = _text(item, "link") or _text(item, "guid")
        title = fix_mojibake(_text(item, "title"))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        out.append(Job(
            source="cryptojobslist",
            external_id=url,
            title=title[:160],
            company=fix_mojibake(_text(item, _DC + "creator")),
            location=fix_mojibake(_text(item, _MEDIA + "location")) or "Unknown",
            url=url,
            posted_at="",
            snippet=clean_snippet(_text(item, "description"), max_chars=400),
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
        log.error("cryptojobslist fetch failed: %s", e)
        err = {"class": type(e).__name__, "message": str(e)[:300]}
    forensic.log_step(
        "cryptojobslist.fetch",
        input={"endpoint": FEED_URL, "max_per_source": cap},
        output={
            "status_code": status_code,
            "count": len(jobs),
            "sample_titles": [j.title[:80] for j in jobs[:3]],
        },
        error=err,
    )
    return jobs
