import { Schema, model, type HydratedDocument } from "mongoose";

export interface TuckAgentConfigAttrs {
  user_id: string;
  /** Section ids the user wants in their daily market report. */
  sections: string[];
  /** Optional companies of interest to highlight in the report. */
  companies: string[];
  /** Whether the daily automatic report is active. */
  enabled: boolean;
}

const tuckAgentConfigSchema = new Schema<TuckAgentConfigAttrs>(
  {
    user_id: { type: String, required: true, unique: true, index: true },
    sections: { type: [String], default: [] },
    companies: { type: [String], default: [] },
    enabled: { type: Boolean, default: true },
  },
  { collection: "tuck_agent_configs", versionKey: false, timestamps: true },
);

export type TuckAgentConfigDocument = HydratedDocument<TuckAgentConfigAttrs>;

export const TuckAgentConfig = model<TuckAgentConfigAttrs>(
  "TuckAgentConfig",
  tuckAgentConfigSchema,
);
