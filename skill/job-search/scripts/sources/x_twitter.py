"""Global X posts via local browser cache, official search, or a chosen actor.

Outside browser mode, no credentials means no network or spending.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import re

import requests

import forensic
from dedupe import Job
from sources._apify import run_actor
from social_seeds import to_api_query
from sources._social import enrich_with_linked_pages, is_external_link
from text_utils import fix_mojibake

log = logging.getLogger(__name__)
DEFAULT_QUERIES = [
    '("we\'re hiring" OR "we are hiring" OR "now hiring" OR "join our team") (engineer OR developer OR designer OR scientist OR researcher) -filter:retweets -filter:replies',
    '(hiring OR "open role" OR "job opening") (remote OR hybrid) (apply OR "DM me") -giveaway -filter:retweets -filter:replies',
]
SNIPPET_MAX_CHARS = 4000
_REQUEST_TIMEOUT_S = 15
_logged_no_credentials = False


def _parse_created(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _title_from_text(text: str) -> str:
    first = text.strip().splitlines()[0] if text.strip() else ""
    title = " ".join(re.sub(r"https?://\S+", "", first).split())
    return title[:100] + ("…" if len(title) > 100 else "")


def _external_from_entities(entities) -> str:
    if not isinstance(entities, dict):
        return ""
    for entry in entities.get("urls") or []:
        if not isinstance(entry, dict):
            continue
        link = entry.get("expanded_url") or entry.get("url") or ""
        if is_external_link(link):
            return link
    return ""


def fetch(filters: dict) -> list[Job]:
    """Use only browser cache when enabled; otherwise query API/explicit actor."""
    if os.environ.get("OINK_SOCIAL_BROWSER_ENABLED", "").strip() == "1":
        try:
            from social_browser import fetch_cached
            return fetch_cached("x_twitter", filters)
        except Exception as exc:
            log.warning("x_twitter: browser cache failed (%s)", type(exc).__name__)
            return []
    global _logged_no_credentials
    jobs: list[Job] = []
    links: dict[str, str] = {}
    skipped = {"no_date": 0, "too_old": 0, "duplicate": 0, "structural": 0, "missing": 0}
    seen = set()
    mode = "none"
    error = None
    inputs = {}
    try:
        bearer = os.environ.get("X_BEARER_TOKEN", "").strip()
        apify_token = os.environ.get("APIFY_TOKEN", "").strip()
        actor = os.environ.get("X_APIFY_ACTOR", "").strip()
        if bearer:
            mode = "api"
        elif apify_token and actor:
            mode = "apify"
        else:
            if not _logged_no_credentials:
                log.info("x_twitter: no X_BEARER_TOKEN and no APIFY_TOKEN+X_APIFY_ACTOR; skipping")
                _logged_no_credentials = True
            forensic.log_step("x_twitter.fetch", output={"reason": "no_credentials", "jobs": 0})
            return []
        queries = filters.get("x_search_queries") or DEFAULT_QUERIES
        cap = int(filters.get("max_per_source", 36))
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=int(filters.get("max_age_hours", 48)))
        inputs = {"queries": queries, "cap": cap}

        def add(item, *, api=False, users=None):
            if not isinstance(item, dict):
                skipped["missing"] += 1
                return
            if item.get("isRetweet") or item.get("noResults"):
                skipped["structural"] += 1
                return
            tweet_id = item.get("id") or item.get("id_str")
            note = item.get("note_tweet") or {}
            text = (note.get("text") or item.get("text")) if api else (
                item.get("fullText") or item.get("full_text") or item.get("text")
            )
            if not tweet_id or not text:
                skipped["missing"] += 1
                return
            tweet_id = str(tweet_id)
            if tweet_id in seen:
                skipped["duplicate"] += 1
                return
            dt = _parse_created(item.get("createdAt") or item.get("created_at"))
            if dt is None:
                skipped["no_date"] += 1
                return
            if dt < cutoff:
                skipped["too_old"] += 1
                return
            author = item.get("author") or {}
            user = item.get("user") or {}
            if api:
                author = (users or {}).get(str(item.get("author_id")), {})
                username = author.get("username") or ""
            else:
                author = author if isinstance(author, dict) else {}
                user = user if isinstance(user, dict) else {}
                username = author.get("userName") or user.get("screen_name") or ""
            name = author.get("name") or ""
            text = fix_mojibake(text)
            link = _external_from_entities(note.get("entities")) or _external_from_entities(item.get("entities"))
            url = f"https://x.com/{username}/status/{tweet_id}" if username else f"https://x.com/i/web/status/{tweet_id}"
            if not api:
                url = item.get("url") or item.get("twitterUrl") or url
            snippet = f"Author: {name} (@{username})\n{text}"
            if link:
                snippet += f"\nLink: {link}"
                links[tweet_id] = link
            jobs.append(Job(
                source="x_twitter", external_id=tweet_id, title=_title_from_text(text),
                company=f"@{username}" if username else "", location="", url=url,
                posted_at=dt.isoformat(), snippet=snippet[:SNIPPET_MAX_CHARS],
            ))
            seen.add(tweet_id)

        if mode == "api":
            start = max(cutoff, now - timedelta(days=7) + timedelta(minutes=1))
            for query in queries:
                if len(jobs) >= cap:
                    break
                response = requests.get(
                    "https://api.x.com/2/tweets/search/recent",
                    headers={"Authorization": f"Bearer {bearer}"},
                    params={
                        "query": to_api_query(query), "max_results": max(10, min(100, cap)),
                        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "tweet.fields": "created_at,author_id,lang,entities,note_tweet",
                        "expansions": "author_id", "user.fields": "username,name",
                    }, timeout=_REQUEST_TIMEOUT_S,
                )
                if response.status_code != 200:
                    code = response.status_code
                    error = {"status_code": code}
                    if code in (401, 403):
                        log.warning("x_twitter: HTTP %s; check X_BEARER_TOKEN / plan", code)
                        break
                    log.warning("x_twitter: recent search returned HTTP %s", code)
                    if code == 429:
                        break
                    continue
                payload = response.json()
                users = {str(user["id"]): user for user in (payload.get("includes") or {}).get("users", [])}
                for item in payload.get("data") or []:
                    add(item, api=True, users=users)
                    if len(jobs) >= cap:
                        break
        elif cap > 0:
            items = run_actor(actor, {"searchTerms": queries, "maxItems": cap, "sort": "Latest"},
                              apify_token, timeout=int(filters.get("apify_run_timeout_s") or 600))
            for item in items:
                add(item)
                if len(jobs) >= cap:
                    break
    except Exception as exc:
        log.warning("x_twitter: fetch failed (%s)", type(exc).__name__)
        error = {"class": type(exc).__name__}
    jobs, enriched = enrich_with_linked_pages(jobs, links, source_label="x_twitter")
    forensic.log_step("x_twitter.fetch", input=inputs, output={
        "jobs": len(jobs), "skipped": skipped, "mode": mode,
        "sample_titles": [job.title for job in jobs[:5]], "enriched": enriched,
    }, error=error)
    return jobs
