"""Global Reddit community posts via local browser cache, public JSON, or OAuth.

Only structural exclusions are applied; the AI scorer decides job relevance.
Transport failures preserve collected posts and never escape fetch().
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import time

import requests

import forensic
from dedupe import Job
from sources._social import enrich_with_linked_pages, first_external_link, is_external_link
from text_utils import fix_mojibake

log = logging.getLogger(__name__)
USER_AGENT = "python:oink-job-search:v1.0 (by /u/oink-job-bot)"
PUBLIC_BASE = "https://www.reddit.com"
OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
DEFAULT_SUBREDDITS = [
    "forhire", "hiring", "jobbit", "remotejs", "PythonJobs",
    "MachineLearningJobs", "DevOpsJobs", "golangjobs", "rustjobs",
]
PAGE_LIMIT = 100
REQUEST_TIMEOUT_S = 15
SNIPPET_MAX_CHARS = 4000


def fetch(filters: dict) -> list[Job]:
    """Use only browser cache when enabled; otherwise fetch subreddit listings."""
    if os.environ.get("OINK_SOCIAL_BROWSER_ENABLED", "").strip() == "1":
        try:
            from social_browser import fetch_cached
            return fetch_cached("reddit", filters)
        except Exception as exc:
            log.warning("reddit: browser cache failed (%s)", type(exc).__name__)
            return []
    jobs: list[Job] = []
    links: dict[str, str] = {}
    skipped = {"stickied": 0, "nsfw": 0, "removed": 0, "too_old": 0}
    pages = 0
    auth = "public"
    error = None
    inputs = {}
    try:
        subs = filters.get("reddit_subreddits") or DEFAULT_SUBREDDITS
        cap = int(filters.get("max_per_source", 36))
        max_pages = int(filters.get("reddit_max_pages", 3))
        cutoff = time.time() - int(filters.get("max_age_hours", 48)) * 3600
        inputs = {"subreddits": subs, "cap": cap, "max_pages": max_pages}
        headers = {"User-Agent": USER_AGENT}
        base = PUBLIC_BASE
        client = os.environ.get("REDDIT_CLIENT_ID", "").strip()
        secret = os.environ.get("REDDIT_CLIENT_SECRET", "").strip()
        if client and secret:
            try:
                response = requests.post(
                    TOKEN_URL, auth=(client, secret),
                    data={"grant_type": "client_credentials"},
                    headers=headers, timeout=REQUEST_TIMEOUT_S,
                )
                if response.status_code != 200:
                    raise ValueError("token rejected")
                token = response.json()["access_token"]
                if not token:
                    raise ValueError("empty token")
                headers["Authorization"] = f"bearer {token}"
                base = OAUTH_BASE
                auth = "oauth"
            except Exception as exc:
                log.warning("reddit: OAuth failed (%s); using public JSON", type(exc).__name__)
        url = f"{base}/r/{'+'.join(subs)}/new" + (".json" if auth == "public" else "")
        after = None
        stop = cap <= 0
        for _ in range(max_pages):
            if stop:
                break
            params = {"limit": PAGE_LIMIT, "raw_json": 1}
            if after:
                params["after"] = after
            response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT_S)
            pages += 1
            if response.status_code != 200:
                log.warning("reddit: listing returned HTTP %s", response.status_code)
                error = {"status_code": response.status_code, "body": response.text[:300]}
                break
            data = response.json()["data"]
            for child in data["children"]:
                post = child["data"]
                created = float(post["created_utc"])
                if created < cutoff:
                    skipped["too_old"] += 1
                    stop = True
                    break
                if post.get("stickied"):
                    skipped["stickied"] += 1
                    continue
                if post.get("over_18"):
                    skipped["nsfw"] += 1
                    continue
                if (post.get("removed_by_category") or
                    post.get("selftext") in ("[removed]", "[deleted]") or
                    post.get("author") == "[deleted]"):
                    skipped["removed"] += 1
                    continue
                title = fix_mojibake(post["title"])
                text = fix_mojibake(post.get("selftext") or "")
                lines = [f"Subreddit: r/{post['subreddit']}"]
                if post.get("link_flair_text"):
                    lines.append(f"Flair: {post['link_flair_text']}")
                lines.extend([f"Title: {title}", text])
                link = ""
                if post.get("is_self") is False:
                    target = post.get("url") or ""
                    lines.append(f"Link: {target}")
                    if is_external_link(target):
                        link = target
                else:
                    link = first_external_link(text)
                external_id = str(post["id"])
                jobs.append(Job(
                    source="reddit", external_id=external_id, title=title[:200],
                    company=f"u/{post['author']}", location="",
                    url=f"{PUBLIC_BASE}{post['permalink']}",
                    posted_at=datetime.fromtimestamp(created, tz=timezone.utc).isoformat(),
                    snippet="\n".join(lines)[:SNIPPET_MAX_CHARS],
                ))
                if link:
                    links[external_id] = link
                if len(jobs) >= cap:
                    stop = True
                    break
            after = data.get("after")
            if not after:
                break
    except Exception as exc:
        log.warning("reddit: fetch failed (%s)", type(exc).__name__)
        error = {"class": type(exc).__name__}
    jobs, enriched = enrich_with_linked_pages(jobs, links, source_label="reddit")
    forensic.log_step("reddit.fetch", input=inputs, output={
        "jobs": len(jobs), "skipped": skipped, "pages": pages, "auth": auth,
        "sample_titles": [job.title for job in jobs[:5]], "enriched": enriched,
    }, error=error)
    return jobs
