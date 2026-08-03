import { Schema, model, type HydratedDocument } from "mongoose";

export interface JoneAgentConfigAttrs {
  user_id: string;
  /** Market summary column keys the user wants Jone to focus on. */
  columns: string[];
  /** watchlist | top_gainers | top_losers */
  group_kind: string;
  /** Analytics My List watchlist id (client-side list) or preset id. */
  watchlist_id: string;
  watchlist_name: string;
  /** CSE symbols in the selected watchlist at save time (empty for presets). */
  watchlist_symbols: string[];
  enabled: boolean;
}

const joneAgentConfigSchema = new Schema<JoneAgentConfigAttrs>(
  {
    user_id: { type: String, required: true, unique: true, index: true },
    columns: { type: [String], default: [] },
    group_kind: { type: String, default: "watchlist" },
    watchlist_id: { type: String, default: "" },
    watchlist_name: { type: String, default: "" },
    watchlist_symbols: { type: [String], default: [] },
    enabled: { type: Boolean, default: true },
  },
  { collection: "jone_agent_configs", versionKey: false, timestamps: true },
);

export type JoneAgentConfigDocument = HydratedDocument<JoneAgentConfigAttrs>;

export const JoneAgentConfig = model<JoneAgentConfigAttrs>(
  "JoneAgentConfig",
  joneAgentConfigSchema,
);
