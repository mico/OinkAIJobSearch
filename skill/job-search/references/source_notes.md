# Source notes and caveats

Reference material for understanding / debugging each source adapter.

## HackerNews "Who is Hiring?"

- **Endpoint**: Algolia search + Firebase item API. Both are free, unauthenticated, rate-friendly.
- **Cadence**: The thread is posted on the 1st of each month around 11:00 AM PT by user `whoishiring`. Posts stay relevant for the whole month.
- **Quality**: High signal, mostly technical roles. ONSITE / REMOTE / VISA tags are a community convention — use the `remote` filter in config to leverage them.
- **Gotchas**:
  - The "latest" thread might be a few days old right after the month rolls over — the adapter's `max_age_hours` is softened (×30) for HN so it keeps working.
  - Posts aren't structured; the adapter takes the first 80 chars of the comment as the "title" and the substring before `|` as the "company". Good enough for a digest.

## Reddit

- **Endpoint**: Combined `/r/a+b/new.json` public listing, or `/r/a+b/new` on `oauth.reddit.com`; newest first, cursor-paginated.
- **Auth**: External Data API access requires Reddit approval. Optional `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET` app-only OAuth; falls back to public JSON if token acquisition fails. With `OINK_SOCIAL_BROWSER_ENABLED=1`, reads the extension cache instead and never calls the API.
- **Rate limits**: At most `reddit_max_pages` (default 3) requests per run, 100 posts per page. Stop on HTTP errors including 429; no retry loop.
- **Fields**: Post ID, title, `u/author`, permalink, UTC creation time; subreddit, flair, selftext and optional external linked-page body in the snippet.
- **Gotchas**: Reddit often 403s datacenter IPs without OAuth. Stickied, NSFW and removed posts are excluded structurally; `[For Hire]` posts are left to the scorer (R9) by design. Global on both fetch backends.
- **Desktop collection**: Load `chrome-extension/` into your signed-in Chrome and pair it with the authenticated loopback receiver; see README setup. Only rendered, timestamped posts are collected. Login/access challenges pause collection; browser access is not permission to scrape or send data to an external scorer.
- **Body recovery**: The combined browser feed may render legacy cards with unloaded selftext. The extension follows captured full permalinks (including the observed title slug) for missing bodies, reads only that post, and preserves fuller observations at the collection cap. Bare-ID URLs can serve an empty SPA shell in inactive tabs.
- **Linked descriptions**: Browser-mode fetch also enriches observed external links via the shared SSRF-safe transport. Failed fetches keep the post; changed job content invalidates stale scores and queued verdicts without changing job identity or first-seen history.

## X (Twitter)

- **Endpoint**: `https://api.x.com/2/tweets/search/recent`, or an explicitly configured `X_APIFY_ACTOR` via the shared Apify transport.
- **Auth**: `X_BEARER_TOKEN` takes precedence; otherwise BOTH `APIFY_TOKEN` + `X_APIFY_ACTOR` are required. No default actor and no network/spending without credentials.
- **Rate limits**: Official recent search uses paid API credits and allows a maximum 7-day lookback (clamped to 7 days minus one minute). One request per query; stop on 401/403/429.
- **Fields**: Tweet ID, author handle, full note-tweet text when present, ISO creation time, post permalink and optional external linked-page body.
- **Gotchas**: Apify actor schemas vary; the adapter supports common tweet-scraper fields and legacy dates. Relevance and self-promotion are handled by scorer R9, not keyword filters. Global on both fetch backends.
- **Desktop collection**: `OINK_SOCIAL_BROWSER_ENABLED=1` switches this adapter exclusively to the local extension cache, even without X credentials. The extension reads loaded search-result posts in inactive tabs; no cookies are exported and no paid transport fallback occurs. Chrome/receiver must be running and the machine awake.
- **Linked descriptions**: Expanded links observed in the DOM are preferred. `t.co` HTTP and zero-delay HTML redirects are resolved before fetching the external body; social-thread destinations and loops are excluded. Original post context and linked description remain within 4,000 characters. Failed/blocked destinations are not evidence of a match.

## RemoteOK (`https://remoteok.com/api`)

- **Endpoint**: Single JSON array at the root URL. The first element is a disclaimer — skip it.
- **Auth**: None needed.
- **Rate limits**: Be kind; once per day is fine.
- **Fields**: `position`, `company`, `location`, `url`, `tags`, `salary_min`, `salary_max`, `description`.
- **Gotchas**: Occasionally returns duplicates. Our dedupe handles that.

## Remotive (`https://remotive.com/api/remote-jobs`)

- **Endpoint**: JSON `{ jobs: [...] }`. Free, no auth.
- **Fields**: `title`, `company_name`, `candidate_required_location`, `url`, `salary`, `publication_date`, `description`, `tags`.

## WeWorkRemotely

- **Endpoint**: Per-category RSS. Standard RSS 2.0.
- **Fields**: `title` ("Company: Role Title"), `link`, `summary`, `published`.
- **Gotchas**: `summary` is HTML — the adapter takes the first 400 chars raw; Telegram formatter strips it further.

## Indeed

Removed. The legacy adapter relied on a hardcoded global query (`q="frontend
developer", l="Bilbao, Spain"`) baked into `config/filters.yaml`. With matching
now per-user-only, there's no equivalent default — Indeed RSS support has also
been quietly deprecated in many regions. To revive: ship a per-user adapter
that reads `profile.search_seeds.indeed` (currently unset by the profile
builder) and add an entry under `defaults.DEFAULTS["sources"]`.

## LinkedIn

**Enable at your own risk.** LinkedIn's TOS explicitly prohibits scraping, and
they litigate.

LinkedIn now runs **per-user only** — `linkedin.fetch_for_user(filters, seeds)`
consumes `profile.search_seeds.linkedin.queries` (up to 3) and returns
deduplicated results. There is no global LinkedIn query; users without an
Opus-built profile get no LinkedIn results.

- **Endpoint**: `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` — the HTML fragment LinkedIn's own page uses for infinite-scroll. No auth required.
- **Useful params**:
  - `keywords` — free-text query
  - `location` — e.g. "Netherlands" or "Greater Amsterdam Area"
  - `f_TPR=r86400` — posted in the last 24h (`r604800` for last week, `r2592000` for last month)
  - `f_WT=2` — remote only
  - `f_E=2,3,4` — experience level (2=entry, 3=associate, 4=mid-senior, 5=director, 6=executive)
  - `start=0` — pagination offset (cards per page ≈ 25)
- **Rate limits**: Very aggressive. 429s appear after ~10 rapid requests. Adapter sleeps 1.5s between calls and skips the rest of the user's batch on 429.
- **HTML fragility**: The CSS selectors `li`, `.base-card`, `h3`, `h4`, `.job-search-card__location` are the current (2025) structure. If LinkedIn changes their markup, parsing breaks — fix selectors in `sources/linkedin.py`.

## Curated boards (AI-delegated): remocate.app / wantapply.com / remoterocketship.com

These three boards publish listings only via HTML — no RSS, no public JSON, and
Remote Rocketship's real feed is paid. Rather than writing brittle BeautifulSoup
selectors, `sources/curated_boards.py` shells out to the Codex CLI once per
enabled board and asks Codex to fetch the page via native live web search/open
and return a strict JSON payload. The result is fed back into
the normal pipeline, so downstream filters (`title_must_match`, `title_exclude`,
dedupe, per-user seen-list) apply as usual.

**Requirements on the machine that runs the cron:**
- The `codex` CLI is on PATH (or `OINK_CODEX_BIN` is set), signed in with
  ChatGPT via `codex login` as the same OS user that runs the bot.
- Network egress to the three domains.

**Usage:** ~1 Codex prompt per enabled board per run — three at most per day.

**Toggles:** `remocate`, `wantapply`, `remoterocketship` under
`defaults.DEFAULTS["sources"]`. All three are OFF by default.
`ai_scrape_timeout_s` (default 180) caps each sub-process.

**Failure modes (all silent — logged at WARN/ERROR, pipeline continues):**
- Codex CLI missing or unauthenticated → board skipped.
- CLI timed out → board skipped for this run.
- JSON parse failure → board skipped; raw output head logged.

**Why not direct HTTP scraping?** Tried it; the three boards change markup
often enough (and ship heavy client-side rendering) that static selectors
rot within weeks. Delegating to an LLM is more resilient and uses the
operator's authenticated ChatGPT account.

## Open-web discovery (Codex native live web search/open)

`sources/web_search.py` runs a single Codex CLI invocation per
orchestrator run with native live web search/open enabled. The agent
forms up to four different queries, picks the most-promising results, and
opens each posting's canonical detail page to extract title,
company, location, URL, and snippet. Results flow through the same dedupe +
post-filter + enrichment pipeline as every other source.

This adapter fills the gap the static-source adapters can't cover:
one-off postings on company career pages, ATS systems (greenhouse.io,
lever.co, ashbyhq.com, workable.com, personio.jobs, recruitee.com, workday,
smartrecruiters.com), regional job boards, engineering-team blogs, and
anywhere else a web search can reach.

**Requirements:**
- Codex CLI on PATH (or `OINK_CODEX_BIN` set), signed in with ChatGPT
  as the bot OS user.
- Network egress for native live web search/open.

**Usage:** one Codex invocation per run, which internally issues
several web searches/page opens. Plan for ~60-120 seconds of wall clock.

**Toggle:** `sources.web_search` in `defaults.DEFAULTS`. ON by default —
gated per-user by whether the profile has `search_seeds.web_search` populated
or the user typed `/prefs`.

**Timeout:** `ai_web_search_timeout_s` (default 300). Falls back to
`ai_scrape_timeout_s` when unset.

**Domain exclusion:** The prompt tells the agent to skip the domains
already covered by dedicated adapters (RemoteOK, Remotive, WeWorkRemotely,
LinkedIn, HackerNews, remocate, wantapply, remoterocketship) so
we don't pay tokens to re-find what other adapters surface for free.

**Failure modes (silent, logged, pipeline continues):**
- CLI missing → returns [].
- CLI timeout / non-zero exit → returns [].
- Non-JSON response → returns [].

## Adding a new source

1. Drop `my_source.py` in `skill/job-search/scripts/sources/`.
2. Export `def fetch(filters: dict) -> list[Job]`.
3. Import it in `search_jobs.py` and add to the `SOURCES` dict.
4. Add a toggle under `sources:` in `defaults.DEFAULTS`.
5. Document quirks here.

## Debugging a bad run

- `python skill/job-search/scripts/search_jobs.py --dry-run` prints every job it finds without posting.
- Set `LOG_LEVEL=DEBUG` in the environment for verbose logs.
- Delete `state/seen.json` to force the next run to treat everything as new (useful for testing).
