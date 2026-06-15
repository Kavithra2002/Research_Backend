import { Schema, model, type HydratedDocument } from "mongoose";

export interface CompanyAttrs {
  slug: string;
  name: string;
  sector: string | null;
  sector_detail: string | null;
  created_at: string;
  updated_at: string;
}

const companySchema = new Schema<CompanyAttrs>(
  {
    slug: { type: String, required: true, unique: true, index: true },
    name: { type: String, required: true, trim: true },
    sector: { type: String, default: null, index: true },
    sector_detail: { type: String, default: null },
    created_at: { type: String, required: true },
    updated_at: { type: String, required: true },
  },
  { collection: "companies", versionKey: false },
);

export type CompanyDocument = HydratedDocument<CompanyAttrs>;

export const Company =
  model<CompanyAttrs>("Company", companySchema);
