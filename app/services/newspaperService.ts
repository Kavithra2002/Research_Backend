import {
  NewsItem,
  type NewsItemCategory,
  type NewsItemType,
} from "../models/NewsItem";
import { logger } from "../utils/logger";
import {
  decodeGoogleNewsUrl,
  isGoogleNewsUrl,
} from "./googleNewsUrlDecoder";

/* ────────────────────────────────────────────────────────────────────────── *
 * Automated market newspaper — RSS aggregation + featured scoring.
 * ────────────────────────────────────────────────────────────────────────── */

export type NewsItemPublic = {
  id: string;
  url: string;
  title: string;
  summary: string;
  imageUrl: string | null;
  sourceName: string;
  publishedAt: string;
  type: NewsItemType;
  category: NewsItemCategory;
  featured: boolean;
  youtubeId: string | null;
};

export type NewspaperPayload = {
  featured: NewsItemPublic[];
  feed: NewsItemPublic[];
  lastRefreshedAt: string | null;
  itemCount: number;
};

type FeedSource = {
  id: string;
  name: string;
  url: string;
  category: NewsItemCategory;
  /** Extra weight toward Featured section. */
  featuredBias: number;
};

const FEED_SOURCES: FeedSource[] = [
  {
    id: "global-markets",
    name: "Global markets",
    url: "https://news.google.com/rss/search?q=global+stock+market+economy+when:7d&hl=en-US&gl=US&ceid=US:en",
    category: "global",
    featuredBias: 1,
  },
  {
    id: "geopolitics",
    name: "Geopolitics",
    url: "https://news.google.com/rss/search?q=geopolitical+conflict+markets+war+when:7d&hl=en-US&gl=US&ceid=US:en",
    category: "geopolitics",
    featuredBias: 3,
  },
  {
    id: "oil-energy",
    name: "Oil & energy",
    url: "https://news.google.com/rss/search?q=crude+oil+price+OPEC+energy+when:7d&hl=en-US&gl=US&ceid=US:en",
    category: "commodities",
    featuredBias: 3,
  },
  {
    id: "fed-rates",
    name: "Rates & inflation",
    url: "https://news.google.com/rss/search?q=Federal+Reserve+interest+rates+inflation+markets+when:7d&hl=en-US&gl=US&ceid=US:en",
    category: "global",
    featuredBias: 2,
  },
  {
    id: "sri-lanka-markets",
    name: "Sri Lanka markets",
    url: "https://news.google.com/rss/search?q=Sri+Lanka+economy+stock+market+when:7d&hl=en-LK&gl=LK&ceid=LK:en",
    category: "local",
    featuredBias: 2,
  },
  {
    id: "cse-colombo",
    name: "Colombo Stock Exchange",
    url: "https://news.google.com/rss/search?q=Colombo+Stock+Exchange+CSE+when:7d&hl=en-LK&gl=LK&ceid=LK:en",
    category: "local",
    featuredBias: 1,
  },
  {
    id: "market-videos",
    name: "Market videos",
    url: "https://news.google.com/rss/search?q=stock+market+analysis+video+when:7d&hl=en-US&gl=US&ceid=US:en",
    category: "global",
    featuredBias: 0,
  },
  {
    id: "bbc-business",
    name: "BBC Business",
    url: "https://feeds.bbci.co.uk/news/business/rss.xml",
    category: "global",
    featuredBias: 1,
  },
  {
    id: "bbc-world",
    name: "BBC World",
    url: "https://feeds.bbci.co.uk/news/world/rss.xml",
    category: "geopolitics",
    featuredBias: 2,
  },
  {
    id: "guardian-business",
    name: "The Guardian Business",
    url: "https://www.theguardian.com/business/rss",
    category: "global",
    featuredBias: 1,
  },
  {
    id: "cnbc-top",
    name: "CNBC",
    url: "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    category: "global",
    featuredBias: 1,
  },
];

const FEATURED_KEYWORDS: Array<{ terms: string[]; weight: number }> = [
  { terms: ["war", "conflict", "invasion", "missile", "geopolit"], weight: 4 },
  { terms: ["oil", "crude", "opec", "petroleum", "energy price"], weight: 4 },
  { terms: ["fed", "interest rate", "inflation", "central bank"], weight: 3 },
  { terms: ["recession", "crash", "selloff", "bear market"], weight: 3 },
  { terms: ["tariff", "sanction", "trade war"], weight: 3 },
  { terms: ["sri lanka", "colombo stock", "cse", "cbsl"], weight: 2 },
  { terms: ["china", "middle east", "ukraine", "gaza"], weight: 2 },
];

const FETCH_TIMEOUT_MS = 12_000;
const OG_IMAGE_TIMEOUT_MS = 8_000;
const OG_HTML_MAX_BYTES = 96_000;
const OG_ENRICH_LIMIT = Number(process.env.NEWSPAPER_THUMBNAIL_LIMIT ?? 20) || 20;
const OG_ENRICH_CONCURRENCY = 5;
const THUMBNAIL_RETRY_MS = 7 * 24 * 60 * 60 * 1000;
const STALE_AFTER_MS = 60 * 60 * 1000;
const FEATURED_LIMIT = 8;
const FEATURED_GEOPOLITICS_MIN = 4;
const FEED_LIMIT = 120;

let lastRefreshAt: string | null = null;
let refreshInFlight: Promise<void> | null = null;
let thumbnailEnrichInFlight: Promise<number> | null = null;

type ParsedRssItem = {
  title: string;
  url: string;
  summary: string;
  imageUrl: string | null;
  sourceName: string;
  publishedAt: Date;
};

function nowIso(): string {
  return new Date().toISOString();
}

function decodeXml(text: string): string {
  return text
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .trim();
}

function extractTag(block: string, tag: string): string {
  const re = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)<\\/${tag}>`, "i");
  const m = re.exec(block);
  return m ? decodeXml(m[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim()) : "";
}

function extractLink(block: string): string {
  const atom = /<link[^>]+href=["']([^"']+)["']/i.exec(block);
  if (atom?.[1]) return atom[1].trim();
  const plain = extractTag(block, "link");
  return plain.trim();
}

function extractImage(block: string, summary: string): string | null {
  const media =
    /<media:content\b[^>]*\burl=["']([^"']+)["']/i.exec(block) ??
    /<media:thumbnail\b[^>]*\burl=["']([^"']+)["']/i.exec(block) ??
    /<enclosure\b[^>]*\burl=["']([^"']+)["']/i.exec(block);
  if (media?.[1]?.startsWith("http")) return media[1];

  const img = /<img[^>]+src=["']([^"']+)["']/i.exec(summary);
  if (img?.[1]?.startsWith("http")) return img[1];
  return null;
}

function stripHtml(html: string): string {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function parseRss(xml: string, defaultSource: string): ParsedRssItem[] {
  const items: ParsedRssItem[] = [];
  const blocks = xml.match(/<item[\s\S]*?<\/item>/gi) ?? [];

  for (const block of blocks) {
    const title = extractTag(block, "title");
    const url = extractLink(block);
    if (!title || !url || !url.startsWith("http")) continue;

    const rawSummary =
      extractTag(block, "description") || extractTag(block, "summary");
    const summary = stripHtml(rawSummary).slice(0, 500);
    const sourceName = extractTag(block, "source") || defaultSource;
    const pubRaw = extractTag(block, "pubDate") || extractTag(block, "published");
    const publishedAt = pubRaw ? new Date(pubRaw) : new Date();
    const imageUrl = extractImage(block, rawSummary);

    items.push({
      title,
      url,
      summary,
      imageUrl,
      sourceName,
      publishedAt: Number.isNaN(publishedAt.getTime()) ? new Date() : publishedAt,
    });
  }
  return items;
}

function normalizeUrl(url: string): string {
  try {
    const u = new URL(url);
    u.hash = "";
    return u.toString();
  } catch {
    return url.trim();
  }
}

function youtubeIdFromUrl(url: string): string | null {
  try {
    const u = new URL(url);
    if (u.hostname.includes("youtu.be")) {
      return u.pathname.replace("/", "") || null;
    }
    if (u.hostname.includes("youtube.com")) {
      return u.searchParams.get("v") ?? u.pathname.split("/").pop() ?? null;
    }
  } catch {
    return null;
  }
  return null;
}

function youtubeThumbUrl(id: string): string {
  return `https://img.youtube.com/vi/${id}/hqdefault.jpg`;
}

function resolveAbsoluteUrl(raw: string, baseUrl: string): string | null {
  const value = raw.trim();
  if (!value) return null;
  try {
    if (value.startsWith("//")) return `https:${value}`;
    return new URL(value, baseUrl).toString();
  } catch {
    return null;
  }
}

function parseOgImageFromHtml(html: string, pageUrl: string): string | null {
  const patterns = [
    /<meta[^>]+property=["']og:image(?::secure_url)?["'][^>]+content=["']([^"']+)["']/gi,
    /<meta[^>]+content=["']([^"']+)["'][^>]+property=["']og:image(?::secure_url)?["']/gi,
    /<meta[^>]+name=["']twitter:image(?::src)?["'][^>]+content=["']([^"']+)["']/gi,
    /<meta[^>]+content=["']([^"']+)["'][^>]+name=["']twitter:image(?::src)?["']/gi,
    /<link[^>]+rel=["']image_src["'][^>]+href=["']([^"']+)["']/gi,
  ];

  for (const pattern of patterns) {
    pattern.lastIndex = 0;
    const match = pattern.exec(html);
    if (match?.[1]) {
      const abs = resolveAbsoluteUrl(decodeXml(match[1]), pageUrl);
      if (abs?.startsWith("http")) return abs;
    }
  }
  return null;
}

async function readHtmlPrefix(res: Response, maxBytes: number): Promise<string> {
  if (!res.body) return "";
  const reader = res.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;

  while (total < maxBytes) {
    const { done, value } = await reader.read();
    if (done || !value) break;
    chunks.push(value);
    total += value.length;
    if (total >= maxBytes) break;
  }

  try {
    await reader.cancel();
  } catch {
    // ignore
  }

  const merged = new Uint8Array(Math.min(total, maxBytes));
  let offset = 0;
  for (const chunk of chunks) {
    const take = Math.min(chunk.length, merged.length - offset);
    if (take <= 0) break;
    merged.set(chunk.subarray(0, take), offset);
    offset += take;
  }

  return new TextDecoder("utf-8", { fatal: false }).decode(merged);
}

async function resolvePublisherUrl(pageUrl: string): Promise<string> {
  if (isGoogleNewsUrl(pageUrl)) {
    try {
      const decoded = await decodeGoogleNewsUrl(pageUrl);
      if (decoded.startsWith("http") && !isGoogleNewsUrl(decoded)) {
        return decoded;
      }
    } catch {
      // fall through
    }
  }
  return pageUrl;
}

async function fetchOgImageFromUrl(pageUrl: string): Promise<string | null> {
  const publisherUrl = await resolvePublisherUrl(pageUrl);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), OG_IMAGE_TIMEOUT_MS);
  try {
    const res = await fetch(publisherUrl, {
      signal: controller.signal,
      redirect: "follow",
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        Accept: "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
      },
    });
    if (!res.ok) return null;

    const contentType = res.headers.get("content-type") ?? "";
    if (!contentType.includes("text/html") && !contentType.includes("xml")) {
      return null;
    }

    const html = await readHtmlPrefix(res, OG_HTML_MAX_BYTES);
    return parseOgImageFromHtml(html, res.url || publisherUrl);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function mapPool<T>(
  items: T[],
  concurrency: number,
  worker: (item: T) => Promise<void>,
): Promise<void> {
  let index = 0;
  const runners = Array.from(
    { length: Math.min(concurrency, items.length) },
    async () => {
      while (index < items.length) {
        const i = index;
        index += 1;
        await worker(items[i]);
      }
    },
  );
  await Promise.all(runners);
}

async function enrichMissingThumbnails(
  resetRetry = false,
): Promise<number> {
  const retryBefore = new Date(Date.now() - THUMBNAIL_RETRY_MS).toISOString();

  if (resetRetry) {
    await NewsItem.updateMany(
      { image_url: null },
      { $set: { thumbnail_enriched_at: null } },
    );
  }

  const candidates = await NewsItem.find({
    image_url: null,
    $or: [
      { thumbnail_enriched_at: null },
      { thumbnail_enriched_at: { $lt: retryBefore } },
    ],
  })
    .sort({ featured: -1, featured_score: -1, published_at: -1 })
    .limit(OG_ENRICH_LIMIT)
    .select({ _id: 1, url: 1, youtube_id: 1 })
    .lean();

  if (candidates.length === 0) return 0;

  let enriched = 0;
  const ts = nowIso();

  await mapPool(candidates, OG_ENRICH_CONCURRENCY, async (doc) => {
    let imageUrl: string | null = null;

    if (doc.youtube_id) {
      imageUrl = youtubeThumbUrl(doc.youtube_id);
    } else if (!isGoogleNewsUrl(doc.url)) {
      imageUrl = await fetchOgImageFromUrl(doc.url);
    }

    await NewsItem.updateOne(
      { _id: doc._id },
      {
        $set: {
          thumbnail_enriched_at: ts,
          ...(imageUrl ? { image_url: imageUrl } : {}),
        },
      },
    );

    if (imageUrl) enriched += 1;
  });

  logger.info(
    `[Newspaper] Thumbnail enrichment: ${enriched}/${candidates.length} resolved.`,
  );
  return enriched;
}

function detectType(url: string, title: string, summary: string): NewsItemType {
  const blob = `${url} ${title} ${summary}`.toLowerCase();
  if (youtubeIdFromUrl(url) || blob.includes("youtube.com") || blob.includes("youtu.be")) {
    return "video";
  }
  if (
    blob.includes("podcast") ||
    blob.includes("spotify.com") ||
    blob.includes("apple.com/podcast")
  ) {
    return "podcast";
  }
  return "article";
}

function featuredScore(
  title: string,
  summary: string,
  publishedAt: Date,
  feedBias: number,
  hasImage: boolean,
): number {
  const blob = `${title} ${summary}`.toLowerCase();
  let score = feedBias;
  if (hasImage) score += 3;
  for (const { terms, weight } of FEATURED_KEYWORDS) {
    if (terms.some((t) => blob.includes(t))) score += weight;
  }
  const ageH = (Date.now() - publishedAt.getTime()) / (1000 * 60 * 60);
  if (ageH <= 24) score += 2;
  else if (ageH <= 72) score += 1;
  return score;
}

type ScoredItem = ParsedRssItem & {
  feed: FeedSource;
  type: NewsItemType;
  youtubeId: string | null;
  score: number;
};

const GEOPOLITICS_TOPIC_TERMS = [
  "war",
  "iran",
  "conflict",
  "invasion",
  "geopolit",
  "middle east",
  "ukraine",
  "gaza",
  "sanction",
  "missile",
  "oil",
  "crude",
  "opec",
  "brent",
  "hormuz",
];

function isGeopoliticsRelevant(item: ScoredItem): boolean {
  if (
    item.feed.category === "geopolitics" ||
    item.feed.category === "commodities"
  ) {
    return true;
  }
  const blob = `${item.title} ${item.summary}`.toLowerCase();
  return GEOPOLITICS_TOPIC_TERMS.some((t) => blob.includes(t));
}

/** Mix geopolitics/commodities headlines with other top market stories. */
function pickFeaturedUrls(ranked: ScoredItem[]): Set<string> {
  const byScore = [...ranked].sort((a, b) => b.score - a.score);
  const geoPool = byScore.filter(isGeopoliticsRelevant);
  const otherPool = byScore.filter((i) => !isGeopoliticsRelevant(i));

  const picked: string[] = [];
  const add = (item: ScoredItem) => {
    if (picked.length >= FEATURED_LIMIT) return;
    const url = normalizeUrl(item.url);
    if (!picked.includes(url)) picked.push(url);
  };

  for (const item of geoPool.slice(0, FEATURED_GEOPOLITICS_MIN)) {
    add(item);
  }

  const othersSorted = otherPool.sort((a, b) => {
    const aImg = a.imageUrl || a.youtubeId ? 1 : 0;
    const bImg = b.imageUrl || b.youtubeId ? 1 : 0;
    if (aImg !== bImg) return bImg - aImg;
    return b.score - a.score;
  });
  for (const item of othersSorted) {
    add(item);
  }

  for (const item of geoPool.slice(FEATURED_GEOPOLITICS_MIN)) {
    add(item);
  }

  for (const item of byScore) {
    add(item);
  }

  return new Set(picked);
}

async function fetchFeedXml(url: string): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      signal: controller.signal,
      headers: {
        "User-Agent":
          "Mozilla/5.0 (compatible; AmbeonNewspaperBot/1.0; +https://ambeon.lk)",
        Accept: "application/rss+xml, application/xml, text/xml, */*",
      },
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.text();
  } finally {
    clearTimeout(timer);
  }
}

function toPublic(doc: {
  _id: unknown;
  url: string;
  title: string;
  summary: string;
  image_url: string | null;
  source_name: string;
  published_at: string;
  type: NewsItemType;
  category: NewsItemCategory;
  featured: boolean;
  youtube_id: string | null;
}): NewsItemPublic {
  return {
    id: String(doc._id),
    url: doc.url,
    title: doc.title,
    summary: doc.summary,
    imageUrl: doc.image_url,
    sourceName: doc.source_name,
    publishedAt: doc.published_at,
    type: doc.type,
    category: doc.category,
    featured: doc.featured,
    youtubeId: doc.youtube_id,
  };
}

export async function refreshNewspaperFeeds(): Promise<{
  inserted: number;
  updated: number;
  feedsFetched: number;
  thumbnailsEnriched: number;
}> {
  const ts = nowIso();
  const parsed: Array<
    ParsedRssItem & {
      feed: FeedSource;
      type: NewsItemType;
      youtubeId: string | null;
      score: number;
    }
  > = [];

  let feedsFetched = 0;
  for (const feed of FEED_SOURCES) {
    try {
      const xml = await fetchFeedXml(feed.url);
      const items = parseRss(xml, feed.name);
      feedsFetched += 1;
      for (const item of items) {
        const type = detectType(item.url, item.title, item.summary);
        const youtubeId = youtubeIdFromUrl(item.url);
        parsed.push({
          ...item,
          feed,
          type,
          youtubeId,
          score: featuredScore(
            item.title,
            item.summary,
            item.publishedAt,
            feed.featuredBias,
            Boolean(item.imageUrl),
          ),
        });
      }
    } catch (err) {
      logger.warn(
        `[Newspaper] Feed ${feed.id} failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  // Rank for featured slots (dedupe by URL first).
  const byUrl = new Map<string, (typeof parsed)[number]>();
  for (const item of parsed) {
    const key = normalizeUrl(item.url);
    const existing = byUrl.get(key);
    if (!existing || item.score > existing.score) byUrl.set(key, item);
  }
  const ranked = [...byUrl.values()];
  const featuredUrls = pickFeaturedUrls(ranked);

  let inserted = 0;
  let updated = 0;

  for (const item of ranked) {
    const url = normalizeUrl(item.url);
    const rssImage = item.imageUrl;
    const youtubeId = item.youtubeId;
    const imageFromRssOrYoutube =
      rssImage ?? (youtubeId ? youtubeThumbUrl(youtubeId) : null);

    const payload = {
      url,
      title: item.title,
      summary: item.summary,
      source_name: item.sourceName,
      source_feed_id: item.feed.id,
      published_at: item.publishedAt.toISOString(),
      type: item.type,
      category: item.feed.category,
      featured: featuredUrls.has(url),
      featured_score: item.score,
      youtube_id: youtubeId,
      fetched_at: ts,
      updated_at: ts,
    };

    const result = await NewsItem.updateOne(
      { url },
      [
        {
          $set: {
            ...payload,
            image_url: imageFromRssOrYoutube
              ? imageFromRssOrYoutube
              : { $ifNull: ["$image_url", null] },
          },
        },
      ],
      { upsert: true },
    );
    if (result.upsertedCount > 0) inserted += 1;
    else if (result.modifiedCount > 0) updated += 1;
  }

  const thumbnailsEnriched = await enrichMissingThumbnails(true);

  lastRefreshAt = ts;
  logger.info(
    `[Newspaper] Refreshed ${feedsFetched} feeds — ${ranked.length} items (${inserted} new, ${updated} updated, ${thumbnailsEnriched} thumbnails).`,
  );

  return { inserted, updated, feedsFetched, thumbnailsEnriched };
}

export async function ensureNewspaperFresh(): Promise<void> {
  if (refreshInFlight) {
    await refreshInFlight;
    return;
  }
  const stale =
    !lastRefreshAt ||
    Date.now() - new Date(lastRefreshAt).getTime() > STALE_AFTER_MS;
  const count = await NewsItem.estimatedDocumentCount();
  if (!stale && count > 0) return;

  refreshInFlight = refreshNewspaperFeeds()
    .then(() => undefined)
    .finally(() => {
      refreshInFlight = null;
    });
  await refreshInFlight;
}

async function maybeBackgroundEnrich(): Promise<void> {
  if (thumbnailEnrichInFlight) return;
  const missing = await NewsItem.countDocuments({
    image_url: null,
    $or: [
      { thumbnail_enriched_at: null },
      {
        thumbnail_enriched_at: {
          $lt: new Date(Date.now() - THUMBNAIL_RETRY_MS).toISOString(),
        },
      },
    ],
  });
  if (missing === 0) return;

  thumbnailEnrichInFlight = enrichMissingThumbnails().finally(() => {
    thumbnailEnrichInFlight = null;
  });
  await thumbnailEnrichInFlight;
}

export async function getNewspaperPayload(
  category?: string,
): Promise<NewspaperPayload> {
  await ensureNewspaperFresh();
  void maybeBackgroundEnrich();

  const filter: Record<string, unknown> = {};
  const cat = category?.trim().toLowerCase();
  if (cat && cat !== "all" && cat !== "featured") {
    filter.category = cat;
  }

  const [docs, featuredDocs] = await Promise.all([
    NewsItem.find(filter)
      .sort({ published_at: -1 })
      .limit(FEED_LIMIT)
      .lean(),
    NewsItem.find({ featured: true })
      .sort({ featured_score: -1, published_at: -1 })
      .limit(FEATURED_LIMIT)
      .lean(),
  ]);

  const all = docs.map((d) => toPublic(d as Parameters<typeof toPublic>[0]));

  const featured = featuredDocs.map((d) =>
    toPublic(d as Parameters<typeof toPublic>[0]),
  );

  const meta = await NewsItem.findOne({})
    .sort({ fetched_at: -1 })
    .select({ fetched_at: 1 })
    .lean();

  return {
    featured,
    feed: all,
    lastRefreshedAt: (meta as { fetched_at?: string } | null)?.fetched_at ?? lastRefreshAt,
    itemCount: all.length,
  };
}

export async function forceRefreshNewspaper(): Promise<{
  inserted: number;
  updated: number;
  feedsFetched: number;
  lastRefreshedAt: string;
}> {
  if (refreshInFlight) await refreshInFlight;
  const result = await refreshNewspaperFeeds();
  return { ...result, lastRefreshedAt: lastRefreshAt ?? nowIso() };
}
