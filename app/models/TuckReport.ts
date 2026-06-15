import { Schema, model, type HydratedDocument } from "mongoose";

export interface TuckReportAttrs {
  user_id: string;
  /** Markdown report content. */
  content: string;
  /** Section ids that were included when the report was generated. */
  sections: string[];
  /** "scheduled" for the daily automation, "manual" for on-demand runs. */
  source: "scheduled" | "manual";
}

const tuckReportSchema = new Schema<TuckReportAttrs>(
  {
    user_id: { type: String, required: true, index: true },
    content: { type: String, required: true },
    sections: { type: [String], default: [] },
    source: { type: String, enum: ["scheduled", "manual"], default: "manual" },
  },
  { collection: "tuck_reports", versionKey: false, timestamps: true },
);

export type TuckReportDocument = HydratedDocument<TuckReportAttrs>;

export const TuckReport = model<TuckReportAttrs>("TuckReport", tuckReportSchema);
