import {
  Schema,
  model,
  type HydratedDocument,
  type Types,
} from "mongoose";

export type ExtractionKeywordScope = "fs" | "drivers" | "quarterly";

export interface ExtractionKeywordAttrs {
  canonical_label: string;
  /** All report wordings used for script search (builtin + user). */
  aliases: string[];
  /** User-added report wordings only (shown in saved updates). */
  user_aliases: string[];
  scope: ExtractionKeywordScope;
  /** true when user added a brand-new line item keyword */
  is_new_keyword: boolean;
  /** true when seeded from extraction script catalog */
  is_builtin: boolean;
  /** Position among data rows in the sheet scope (new keywords only). */
  line_order: number | null;
  created_by: Types.ObjectId | null;
  created_by_user_id: string | null;
  created_at: Date;
  updated_at: Date;
}

const extractionKeywordSchema = new Schema<ExtractionKeywordAttrs>(
  {
    canonical_label: {
      type: String,
      required: true,
      trim: true,
      index: true,
    },
    aliases: {
      type: [String],
      default: [],
    },
    user_aliases: {
      type: [String],
      default: [],
    },
    scope: {
      type: String,
      enum: ["fs", "drivers", "quarterly"],
      required: true,
      index: true,
    },
    is_new_keyword: {
      type: Boolean,
      default: false,
    },
    is_builtin: {
      type: Boolean,
      default: false,
    },
    line_order: {
      type: Number,
      default: null,
      index: true,
    },
    created_by: {
      type: Schema.Types.ObjectId,
      ref: "User",
      default: null,
    },
    created_by_user_id: {
      type: String,
      default: null,
    },
  },
  {
    collection: "extraction_keywords",
    timestamps: { createdAt: "created_at", updatedAt: "updated_at" },
    versionKey: false,
    toJSON: {
      virtuals: false,
      transform: (_doc, ret) => {
        const out = ret as Record<string, unknown> & { _id?: unknown };
        if (out._id !== undefined) out._id = String(out._id);
        return out;
      },
    },
  },
);

extractionKeywordSchema.index(
  { canonical_label: 1, scope: 1 },
  { unique: true },
);

export type ExtractionKeywordDocument =
  HydratedDocument<ExtractionKeywordAttrs>;

export const ExtractionKeyword = model<ExtractionKeywordAttrs>(
  "ExtractionKeyword",
  extractionKeywordSchema,
);
