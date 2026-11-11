"""Shared external-link transport and best-effort enrichment for social posts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from html.parser import HTMLParser
import logging
import re
from urllib.parse import urljoin, urlparse

from dedupe import Job

log = logging.getLogger(__name__)
_SOCIAL_HOSTS = ("reddit.com", "redd.it", "x.com", "twitter.com", "t.co")
SNIPPET_MAX_CHARS = 4000


def is_external_link(url: str, *, allow_shortener: bool = False) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme in ("http", "https") and bool(host) and (
            (allow_shortener and host == "t.co") or not any(
                host == h or host.endswith("." + h) for h in _SOCIAL_HOSTS
            )
        )
    except ValueError:
        return False


def first_external_link(text: str, *, allow_shortener: bool = False) -> str:
    for match in re.finditer(r"https?://\S+", text):
        link = match.group().rstrip(")].,")
        if is_external_link(link, allow_shortener=allow_shortener):
            return link
    return ""

class _RefreshTarget(HTMLParser):
    def __init__(self):
        super().__init__()
        self.target = ""

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag.lower() == "meta" and (values.get("http-equiv") or "").lower() == "refresh":
            match = re.fullmatch(r"\s*0(?:\.0+)?\s*;\s*url\s*=\s*(.+?)\s*",
                                 values.get("content") or "", re.IGNORECASE)
            if match and not self.target:
                self.target = match[1].strip("\"'")


def _resolve_shortener(link: str) -> str:
    """Resolve t.co's HTTP or zero-delay HTML redirect without fetching a forum."""
    from safe_url import safe_request
    from sources._detail_fetch import DEFAULT_HEADERS

    seen = set()
    try:
        for _ in range(5):
            if urlparse(link).hostname != "t.co":
                return link if is_external_link(link) else ""
            if link in seen:
                return ""
            seen.add(link)
            response = safe_request("GET", link, headers=DEFAULT_HEADERS,
                                    timeout=15, max_redirects=0)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    target = response.headers.get("Location", "")
                elif response.status_code == 200:
                    parser = _RefreshTarget()
                    parser.feed(response.text)
                    target = parser.target
                else:
                    return ""
            finally:
                response.close()
            if not target:
                return ""
            link = urljoin(link, target)
    except Exception as exc:
        log.warning("social: short-link resolution failed (%s)", type(exc).__name__)
    return ""



def enrich_with_linked_pages(
    jobs: list[Job], links: dict[str, str], *, max_chars: int = 2500,
    source_label: str,
) -> tuple[list[Job], int]:
    if not jobs or not links:
        return jobs, 0
    try:
        from sources._detail_fetch import fetch_many_bodies

        targets = {link: link for link in links.values()}
        shorteners = [link for link in targets if urlparse(link).hostname == "t.co"]
        if shorteners:
            with ThreadPoolExecutor(max_workers=6) as pool:
                targets.update(zip(shorteners, pool.map(_resolve_shortener, shorteners)))
        urls = list(dict.fromkeys(target for target in targets.values() if target))
        if not urls:
            return jobs, 0
        bodies = fetch_many_bodies(urls, max_chars=max_chars, workers=6)
        enriched = 0
        result = []
        for job in jobs:
            link = links.get(job.external_id, "")
            body = bodies.get(targets.get(link, ""), "")
            if body:
                header = "\n\n--- Linked page (" + link + ") ---\n"
                # Reserve original author/title context, but never let a full
                # social post crowd the linked description out of the snippet.
                body_budget = SNIPPET_MAX_CHARS - len(header) - min(len(job.snippet), 1000)
                linked_body = body[:max(0, min(max_chars, body_budget))]
                if linked_body:
                    context_budget = SNIPPET_MAX_CHARS - len(header) - len(linked_body)
                    job = replace(job, snippet=job.snippet[:context_budget] + header + linked_body)
                    enriched += 1
            result.append(job)
        return result, enriched
    except Exception as exc:
        log.warning("%s: linked-page enrichment failed (%s)", source_label, type(exc).__name__)
        return jobs, 0
