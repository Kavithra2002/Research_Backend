import {
  Schema,
  model,
  type HydratedDocument,
  type Types,
} from "mongoose";

export interface ReportSelectionAttrs {
  company: string;
  report_type: string;
  file_name: string;
  selected_by: Types.ObjectId | null;
  selected_by_user_id: string | null;
  created_at: Date;
  updated_at: Date;
}

const reportSelectionSchema = new Schema<ReportSelectionAttrs>(
  {
    company: {
      type: String,
      required: true,
      trim: true,
      index: true,
    },
    report_type: {
      type: String,
      required: true,
      trim: true,
    },
    file_name: {
      type: String,
      required: true,
      trim: true,
    },
    selected_by: {
      type: Schema.Types.ObjectId,
      ref: "User",
      default: null,
    },
    selected_by_user_id: {
      type: String,
      default: null,
    },
  },
  {
    collection: "report_selections",
    timestamps: { createdAt: "created_at", updatedAt: "updated_at" },
    versionKey: false,
    toJSON: {
      virtuals: false,
      transform: (_doc, ret) => {
        const out = ret as Record<string, unknown> & { _id?: unknown };
        if (out._id !== undefined) out._id = String(out._id);
        return out;
      },
    },
  },
);

reportSelectionSchema.index(
  { company: 1, report_type: 1, file_name: 1 },
  { unique: true },
);

export type ReportSelectionDocument = HydratedDocument<ReportSelectionAttrs>;

export const ReportSelection = model<ReportSelectionAttrs>(
  "ReportSelection",
  reportSelectionSchema,
);
