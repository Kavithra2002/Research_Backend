import {
  Schema,
  model,
  type HydratedDocument,
  type Types,
} from "mongoose";

export interface CompanyGroupAttrs {
  name: string;
  description: string;
  companies: string[];
  symbols: string[];
  created_by: Types.ObjectId | null;
  created_by_user_id: string | null;
  created_at: Date;
  updated_at: Date;
}

const companyGroupSchema = new Schema<CompanyGroupAttrs>(
  {
    name: {
      type: String,
      required: true,
      trim: true,
      index: true,
    },
    description: {
      type: String,
      default: "",
      trim: true,
    },
    companies: {
      type: [String],
      default: [],
    },
    symbols: {
      type: [String],
      default: [],
    },
    created_by: {
      type: Schema.Types.ObjectId,
      ref: "User",
      default: null,
    },
    created_by_user_id: {
      type: String,
      default: null,
    },
  },
  {
    collection: "company_groups",
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

companyGroupSchema.index({ name: 1 }, { unique: true });

export type CompanyGroupDocument = HydratedDocument<CompanyGroupAttrs>;

export const CompanyGroup = model<CompanyGroupAttrs>(
  "CompanyGroup",
  companyGroupSchema,
);
