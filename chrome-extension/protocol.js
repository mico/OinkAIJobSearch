export const RECEIVER_ORIGIN = "http://127.0.0.1:8766";
export const SOURCE_NAMES = ["reddit", "x_twitter"];
export const DEFAULT_INTERVAL_MINUTES = 120;
export const MAX_PAGE_MS = 45_000;
export const MAX_RUN_MS = 10 * 60_000;
export const MAX_BATCH_BYTES = 900_000;

export function validToken(value) {
  return typeof value === "string" && /^[\x21-\x7e]{32,512}$/.test(value);
}

export function normalizeSettings(value = {}) {
  const token = typeof value.token === "string" ? value.token : "";
  const interval = Number(value.intervalMinutes);
  return {
    token,
    enabled: value.enabled === true && validToken(token),
    intervalMinutes: Number.isInteger(interval) && interval >= 15 && interval <= 1440
      ? interval : DEFAULT_INTERVAL_MINUTES,
  };
}

export function normalizeConfig(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Receiver returned an invalid configuration.");
  }
  const { reddit_subreddits: subreddits, x_search_queries: queries } = value;
  if (!Array.isArray(subreddits) || subreddits.length > 50
      || subreddits.some((item) => typeof item !== "string" || !/^[A-Za-z0-9_]{1,21}$/.test(item))) {
    throw new Error("Receiver must configure at most 50 valid subreddit names.");
  }
  if (!Array.isArray(queries) || queries.length > 12
      || queries.some((item) => typeof item !== "string" || !item.trim() || item.length > 500)) {
    throw new Error("Receiver must configure at most 12 nonempty X queries (500 characters each).");
  }
  if (!Number.isInteger(value.max_per_source) || value.max_per_source < 1 || value.max_per_source > 100
      || !Number.isFinite(value.max_age_hours) || value.max_age_hours <= 0 || value.max_age_hours > 168) {
    throw new Error("Receiver returned an invalid collection count or age limit.");
  }
  return {
    reddit_subreddits: [...new Set(subreddits)],
    x_search_queries: [...new Set(queries.map((query) => query.trim()))],
    max_per_source: value.max_per_source,
    max_age_hours: value.max_age_hours,
  };
}

export function sourcePages(source, config) {
  if (source === "reddit") {
    return config.reddit_subreddits.length
      ? [`https://www.reddit.com/r/${config.reddit_subreddits.join("+")}/new/`] : [];
  }
  if (source === "x_twitter") {
    return config.x_search_queries.map((query) => {
      const url = new URL("https://x.com/search");
      url.searchParams.set("q", query);
      url.searchParams.set("f", "live");
      return url.href;
    });
  }
  throw new Error("Unsupported source.");
}

export function normalizePost(source, value, config, now = Date.now()) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const string = (key, limit) => typeof value[key] === "string" ? value[key].trim().slice(0, limit) : "";
  const id = string("id", 80);
  const author = string("author", 80);
  const text = string("text", 8000);
  const title = string("title", 300);
  const date = Date.parse(string("created_at", 100));
  if (!id || !author || !(title || text) || !Number.isFinite(date)
      || date > now + 5 * 60_000 || date < now - config.max_age_hours * 3_600_000) return null;
  let observed;
  try { observed = new URL(value.url); } catch { return null; }
  if (observed.protocol !== "https:" || observed.username || observed.password || observed.port) return null;
  let url;
  let subreddit = "";
  if (source === "reddit") {
    if (!["www.reddit.com", "old.reddit.com", "reddit.com"].includes(observed.hostname)) return null;
    const match = observed.pathname.match(/^\/r\/([A-Za-z0-9_]{1,21})\/comments\/([a-z0-9]+)(?:\/[^/]+)?\/?$/i);
    if (!match || match[2].toLowerCase() !== id.toLowerCase() || !/^[A-Za-z0-9_-]{1,40}$/.test(author)) return null;
    subreddit = match[1];
    // Keep the observed title slug: bare-ID URLs can serve an empty SPA shell
    // in inactive tabs instead of the server-rendered post body.
    url = `https://www.reddit.com${observed.pathname.replace(/\/?$/, "/")}`;
  } else if (source === "x_twitter") {
    if (observed.hostname !== "x.com") return null;
    const match = observed.pathname.match(/^\/([A-Za-z0-9_]{1,15})\/status\/(\d+)(?:\/|$)/);
    if (!match || match[2] !== id || match[1].toLowerCase() !== author.toLowerCase()) return null;
    url = `https://x.com/${match[1]}/status/${id}`;
  } else return null;
  let external = "";
  try {
    const candidate = new URL(string("external_url", 2000));
    if (["https:", "http:"].includes(candidate.protocol) && !candidate.username && !candidate.password) {
      external = candidate.href;
    }
  } catch { /* An external link is optional. */ }
  return {
    id: source === "reddit" ? id.toLowerCase() : id,
    title,
    text,
    author,
    display_name: string("display_name", 200),
    url,
    created_at: new Date(date).toISOString(),
    subreddit,
    external_url: external,
  };
}

export function postBatches(source, posts) {
  const encoder = new TextEncoder();
  const batches = [];
  let batch = [];
  for (const post of posts) {
    const next = [...batch, post];
    if (next.length > 100 || encoder.encode(JSON.stringify({ source, posts: next })).length > MAX_BATCH_BYTES) {
      if (!batch.length) throw new Error("A rendered post exceeds the receiver request limit.");
      batches.push(batch);
      batch = [post];
    } else batch = next;
  }
  if (batch.length) batches.push(batch);
  return batches;
}
