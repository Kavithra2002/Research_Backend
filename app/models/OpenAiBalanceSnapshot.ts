import { Schema, model, type HydratedDocument } from "mongoose";

/**
 * A single, system-wide OpenAI credit-balance snapshot. OpenAI exposes no API
 * for the live remaining balance, so an admin records the dashboard balance
 * here (and the date it was true). The usage widget then subtracts real spend
 * since that date to show a live "remaining" estimate.
 *
 * This is a singleton collection — we always read/write the most recent doc.
 * Storing it in the DB (instead of backend/.env) means a top-up can be entered
 * straight from the UI and takes effect immediately, without a server restart.
 */
export interface OpenAiBalanceSnapshotAttrs {
  /** Dashboard balance in USD at the moment it was recorded. */
  balance: number;
  /** ISO date (YYYY-MM-DD, UTC) the balance was accurate. */
  asOf: string;
  /** user_id of the admin who recorded it (for audit). */
  updatedBy?: string | null;
}

const openAiBalanceSnapshotSchema = new Schema<OpenAiBalanceSnapshotAttrs>(
  {
    balance: { type: Number, required: true },
    asOf: { type: String, required: true },
    updatedBy: { type: String, default: null },
  },
  {
    collection: "openai_balance_snapshots",
    versionKey: false,
    timestamps: true,
  },
);

export type OpenAiBalanceSnapshotDocument =
  HydratedDocument<OpenAiBalanceSnapshotAttrs>;

export const OpenAiBalanceSnapshot = model<OpenAiBalanceSnapshotAttrs>(
  "OpenAiBalanceSnapshot",
  openAiBalanceSnapshotSchema,
);
