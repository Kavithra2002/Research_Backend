import { Schema, model, type HydratedDocument } from "mongoose";

export type NewsItemType = "article" | "video" | "podcast";
export type NewsItemCategory = "global" | "local" | "commodities" | "geopolitics";

export interface NewsItemAttrs {
  url: string;
  title: string;
  summary: string;
  image_url: string | null;
  source_name: string;
  source_feed_id: string;
  published_at: string;
  type: NewsItemType;
  category: NewsItemCategory;
  featured: boolean;
  featured_score: number;
  youtube_id: string | null;
  fetched_at: string;
  updated_at: string;
  /** Last time we tried to resolve og:image for this row. */
  thumbnail_enriched_at: string | null;
}

const newsItemSchema = new Schema<NewsItemAttrs>(
  {
    url: { type: String, required: true, unique: true, index: true },
    title: { type: String, required: true, trim: true },
    summary: { type: String, default: "" },
    image_url: { type: String, default: null },
    source_name: { type: String, default: "" },
    source_feed_id: { type: String, required: true, index: true },
    published_at: { type: String, required: true, index: true },
    type: {
      type: String,
      enum: ["article", "video", "podcast"],
      default: "article",
    },
    category: {
      type: String,
      enum: ["global", "local", "commodities", "geopolitics"],
      default: "global",
      index: true,
    },
    featured: { type: Boolean, default: false, index: true },
    featured_score: { type: Number, default: 0 },
    youtube_id: { type: String, default: null },
    fetched_at: { type: String, required: true },
    updated_at: { type: String, required: true },
    thumbnail_enriched_at: { type: String, default: null },
  },
  { collection: "news_items", versionKey: false },
);

export type NewsItemDocument = HydratedDocument<NewsItemAttrs>;

export const NewsItem = model<NewsItemAttrs>("NewsItem", newsItemSchema);
