import { Schema, model, type HydratedDocument } from "mongoose";

export interface MarianReportAttrs {
  user_id: string;
  /** Markdown Daily Market Wrap content. */
  content: string;
  /** Component ids included when the report was generated. */
  sections: string[];
  /** "scheduled" for the daily automation, "manual" for on-demand runs. */
  source: "scheduled" | "manual";
}

const marianReportSchema = new Schema<MarianReportAttrs>(
  {
    user_id: { type: String, required: true, index: true },
    content: { type: String, required: true },
    sections: { type: [String], default: [] },
    source: { type: String, enum: ["scheduled", "manual"], default: "manual" },
  },
  { collection: "marian_reports", versionKey: false, timestamps: true },
);

export type MarianReportDocument = HydratedDocument<MarianReportAttrs>;

export const MarianReport = model<MarianReportAttrs>(
  "MarianReport",
  marianReportSchema,
);
