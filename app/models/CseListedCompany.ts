import { Schema, model, type HydratedDocument } from "mongoose";

export interface CseListedCompanyAttrs {
  symbol: string;
  name: string;
  synced_at: Date;
}

const cseListedCompanySchema = new Schema<CseListedCompanyAttrs>(
  {
    symbol: { type: String, required: true, unique: true, index: true },
    name: { type: String, required: true, trim: true, index: true },
    synced_at: { type: Date, required: true },
  },
  { collection: "cse_listed_companies", versionKey: false },
);

export type CseListedCompanyDocument = HydratedDocument<CseListedCompanyAttrs>;

export const CseListedCompany = model<CseListedCompanyAttrs>(
  "CseListedCompany",
  cseListedCompanySchema,
);
