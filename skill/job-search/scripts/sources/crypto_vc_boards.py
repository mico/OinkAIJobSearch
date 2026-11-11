"""Crypto VC + L1-ecosystem portfolio job boards hosted on Getro.

Module key: ``crypto_vc_boards``

Many crypto investors and chain foundations publish a "jobs across our
portfolio" board on Getro (``jobs.<fund>.<tld>``). Each one aggregates the
career pages of dozens of crypto companies, with the ORIGINAL ATS link
(Ashby / Greenhouse / Lever / …) per posting — high-signal, low-spam roles
that rarely reach the general boards.

Integration choice (probed 2026-11-11):
  - Every board is a Next.js page that server-renders the 20 newest postings
    into ``<script id="__NEXT_DATA__">`` at
    ``props.pageProps.initialState.jobs.found`` (newest ``createdAt`` first),
    with title, organization name + industry tags, locations, work mode,
    seniority, skills, compensation and the ATS ``url``.
  - A plain GET of ``https://<board>/jobs`` is all we do — no private API,
    no detail pages. Boards are fetched sequentially with a pause; Getro's
    robots.txt allows ``User-agent: *``.

Board list: ``DEFAULT_BOARDS`` below, overridable via the
``crypto_vc_boards`` filters key (list of hostnames). Consider.com-hosted
boards (Paradigm, Pantera, a16z) use a different, POST-only platform and are
NOT covered here. Each board contributes up to ``crypto_vc_boards_per_board``
postings (default 12); no pre-filtering — the AI scorer is the matching gate.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

import requests

from dedupe import Job
from text_utils import clean_snippet, fix_mojibake
import forensic

log = logging.getLogger(__name__)

UA = {"User-Agent": "FindJobs-Bot/1.0 (+https://github.com/; personal job-alert)"}

# hostname → human label. Verified Getro boards (200 + __NEXT_DATA__ network id).
DEFAULT_BOARDS: dict[str, str] = {
    "jobs.solana.com":          "Solana ecosystem",
    "jobs.avax.network":        "Avalanche ecosystem",
    "jobs.multicoin.capital":   "Multicoin Capital portfolio",
    "jobs.dragonfly.xyz":       "Dragonfly portfolio",
    "jobs.electriccapital.com": "Electric Capital portfolio",
    "jobs.coinfund.io":         "CoinFund portfolio",
    "jobs.variant.fund":        "Variant portfolio",
    "jobs.placeholder.vc":      "Placeholder portfolio",
    "jobs.framework.ventures":  "Framework Ventures portfolio",
}
DEFAULT_PER_BOARD = 12
BOARD_PAUSE_S = 1.5

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(?P<json>.*?)</script>', re.S,
)


def _board_jobs_raw(html: str) -> list[dict]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return []
    data = json.loads(m.group("json"))
    found = (
        data.get("props", {}).get("pageProps", {})
        .get("initialState", {}).get("jobs", {}).get("found")
    )
    return found if isinstance(found, list) else []


def _fmt_comp(j: dict) -> str:
    lo, hi = j.get("compensationAmountMinCents"), j.get("compensationAmountMaxCents")
    cur = j.get("compensationCurrency") or ""
    try:
        lo = int(lo) // 100 if lo else None
        hi = int(hi) // 100 if hi else None
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    fmt = lambda v: f"{v // 1000}k" if v >= 1000 else str(v)  # noqa: E731
    rng = f"{fmt(lo)}–{fmt(hi)}" if lo and hi else f"{fmt(lo or hi)}+"
    return f"{rng} {cur}".strip()


def _iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def parse_board(html: str, board_label: str) -> list[Job]:
    out: list[Job] = []
    for j in _board_jobs_raw(html):
        url = (j.get("url") or "").strip()
        title = fix_mojibake((j.get("title") or "").strip())
        if not url or not title or j.get("id") is None:
            continue
        org = j.get("organization") or {}
        company = fix_mojibake((org.get("name") or "").strip())
        locations = [str(x) for x in (j.get("locations") or []) if x]
        work_mode = (j.get("workMode") or "").replace("_", " ")
        location = ", ".join(locations) or ("Remote" if work_mode == "remote" else "")
        salary = _fmt_comp(j)
        bits = [
            f"{title} at {company}" if company else title,
            f"via {board_label}",
            f"Work mode: {work_mode}" if work_mode else "",
            f"Seniority: {j['seniority'].replace('_', ' ')}" if j.get("seniority") else "",
            "Industry: " + ", ".join(org.get("industryTags") or [])[:160] if org.get("industryTags") else "",
            "Skills: " + ", ".join(j.get("skills") or [])[:160] if j.get("skills") else "",
            salary,
        ]
        out.append(Job(
            source="crypto_vc_boards",
            external_id=str(j["id"]),
            title=title[:160],
            company=company,
            location=location or "Unknown",
            url=url,
            posted_at=_iso(j.get("createdAt")),
            snippet=clean_snippet(" · ".join(b for b in bits if b), max_chars=400),
            salary=salary,
        ))
    return out


def _boards_from_filters(filters: dict) -> dict[str, str]:
    override = filters.get("crypto_vc_boards")
    if isinstance(override, (list, tuple)) and override:
        return {str(h).strip(): DEFAULT_BOARDS.get(str(h).strip(), str(h).strip())
                for h in override if str(h).strip()}
    return dict(DEFAULT_BOARDS)


def fetch(filters: dict) -> list[Job]:
    per_board = max(1, int(filters.get("crypto_vc_boards_per_board") or DEFAULT_PER_BOARD))
    boards = _boards_from_filters(filters)
    jobs: list[Job] = []
    seen: set[str] = set()
    per_board_meta: list[dict] = []
    for i, (host, label) in enumerate(boards.items()):
        url = f"https://{host}/jobs"
        status_code = None
        err = None
        count = 0
        try:
            r = requests.get(url, headers=UA, timeout=25)
            status_code = r.status_code
            r.raise_for_status()
            for job in parse_board(r.text, label)[:per_board]:
                # Portfolio boards overlap (same company backed by two funds).
                if job.url in seen:
                    continue
                seen.add(job.url)
                jobs.append(job)
                count += 1
        except (requests.RequestException, ValueError) as e:
            log.error("crypto_vc_boards %s fetch failed: %s", host, e)
            err = {"class": type(e).__name__, "message": str(e)[:200]}
        per_board_meta.append({"board": host, "status_code": status_code,
                               "count": count, "error": err})
        if i < len(boards) - 1:
            time.sleep(BOARD_PAUSE_S)
    forensic.log_step(
        "crypto_vc_boards.fetch",
        input={"boards": list(boards), "per_board": per_board},
        output={
            "count": len(jobs),
            "per_board": per_board_meta,
            "sample_titles": [j.title[:80] for j in jobs[:3]],
        },
    )
    return jobs
