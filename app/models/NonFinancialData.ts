import { Schema, model, type HydratedDocument } from "mongoose";

export interface NonFinancialMetric {
  key: string;
  title: string;
  hint?: string;
  found: boolean;
  value: string;
  detail: string;
  pages: number[];
}

export interface NonFinancialCategory {
  key: string;
  title: string;
  metrics: NonFinancialMetric[];
}

export interface NonFinancialDataAttrs {
  company_slug: string;
  company_name: string;
  year: number | null;
  report_key: string;
  report_group: string | null;
  reporting_year: string | null;
  company_overview: string;
  categories: NonFinancialCategory[];
  found_count: number;
  total_count: number;
  source_pdf: string | null;
  extraction_model: string | null;
  extracted_at: string | null;
  usage: Record<string, unknown> | null;
  uploaded_at: string;
}

const nonFinancialDataSchema = new Schema<NonFinancialDataAttrs>(
  {
    company_slug: { type: String, required: true, index: true },
    company_name: { type: String, required: true },
    year: { type: Number, default: null, index: true },
    report_key: { type: String, required: true },
    report_group: { type: String, default: null },
    reporting_year: { type: String, default: null },
    company_overview: { type: String, default: "" },
    categories: { type: Schema.Types.Mixed, default: [] },
    found_count: { type: Number, default: 0 },
    total_count: { type: Number, default: 0 },
    source_pdf: { type: String, default: null },
    extraction_model: { type: String, default: null },
    extracted_at: { type: String, default: null },
    usage: { type: Schema.Types.Mixed, default: null },
    uploaded_at: { type: String, required: true },
  },
  { collection: "non_financial_data", versionKey: false },
);

nonFinancialDataSchema.index(
  { company_slug: 1, report_key: 1 },
  { unique: true, name: "uniq_nf_company_reportkey" },
);

export type NonFinancialDataDocument = HydratedDocument<NonFinancialDataAttrs>;

export const NonFinancialData = model<NonFinancialDataAttrs>(
  "NonFinancialData",
  nonFinancialDataSchema,
);
