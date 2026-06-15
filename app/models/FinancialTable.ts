import { Schema, model, type HydratedDocument } from "mongoose";

export type ReportType = "annual" | "quarterly";

export interface FinancialTableRow {
  cells: string[];
  style?: string;
}

export interface FinancialTableAttrs {
  company_slug: string;
  company_name: string;
  year: number | null;
  report_type: ReportType;
  report_group: string | null;
  report_key: string;
  quarter: string | null;
  period_label: string | null;
  statement_key: string;
  statement_title: string;
  statement_label: string | null;
  table_index: number;
  caption: string | null;
  preamble: string;
  footnotes: string;
  header_rows: string[][];
  rows: FinancialTableRow[];
  row_count: number;
  source_pdf: string | null;
  extraction_model: string | null;
  extraction_status: string;
  extracted_at: string | null;
  uploaded_at: string;
}

const financialTableSchema = new Schema<FinancialTableAttrs>(
  {
    company_slug: { type: String, required: true, index: true },
    company_name: { type: String, required: true },
    year: { type: Number, default: null, index: true },
    report_type: {
      type: String,
      required: true,
      enum: ["annual", "quarterly"],
      index: true,
    },
    report_group: { type: String, default: null },
    report_key: { type: String, required: true },
    quarter: { type: String, default: null },
    period_label: { type: String, default: null },
    statement_key: { type: String, required: true },
    statement_title: { type: String, required: true },
    statement_label: { type: String, default: null },
    table_index: { type: Number, required: true },
    caption: { type: String, default: null },
    preamble: { type: String, default: "" },
    footnotes: { type: String, default: "" },
    header_rows: { type: [[String]], default: [] },
    rows: { type: Schema.Types.Mixed, default: [] },
    row_count: { type: Number, default: 0 },
    source_pdf: { type: String, default: null },
    extraction_model: { type: String, default: null },
    extraction_status: { type: String, default: "ok" },
    extracted_at: { type: String, default: null },
    uploaded_at: { type: String, required: true },
  },
  { collection: "financial_tables", versionKey: false },
);

financialTableSchema.index(
  {
    company_slug: 1,
    report_type: 1,
    report_key: 1,
    statement_key: 1,
    table_index: 1,
  },
  { unique: true, name: "uniq_company_type_reportkey_statement_table" },
);

financialTableSchema.index(
  { company_slug: 1, report_type: 1, year: 1 },
  { name: "lookup_company_type_year" },
);

export type FinancialTableDocument = HydratedDocument<FinancialTableAttrs>;

export const FinancialTable = model<FinancialTableAttrs>(
  "FinancialTable",
  financialTableSchema,
);
