import { Schema, model, type HydratedDocument } from "mongoose";

export interface MarianAgentConfigAttrs {
  user_id: string;
  /** Report component ids (table + chart) the user wants in their Daily Market Wrap. */
  sections: string[];
  /** Whether the daily automatic wrap is active. */
  enabled: boolean;
}

const marianAgentConfigSchema = new Schema<MarianAgentConfigAttrs>(
  {
    user_id: { type: String, required: true, unique: true, index: true },
    sections: { type: [String], default: [] },
    enabled: { type: Boolean, default: true },
  },
  { collection: "marian_agent_configs", versionKey: false, timestamps: true },
);

export type MarianAgentConfigDocument =
  HydratedDocument<MarianAgentConfigAttrs>;

export const MarianAgentConfig = model<MarianAgentConfigAttrs>(
  "MarianAgentConfig",
  marianAgentConfigSchema,
);
