import { Schema, model, type HydratedDocument, type Types } from "mongoose";

export interface SessionAttrs {
  _id: string;
  user_id: Types.ObjectId;
  user_business_id: string;
  expires_at: Date;
  created_at: Date;
  ip: string | null;
  user_agent: string | null;
}

const sessionSchema = new Schema<SessionAttrs>(
  {
    _id: { type: String, required: true },
    user_id: {
      type: Schema.Types.ObjectId,
      ref: "User",
      required: true,
      index: true,
    },
    user_business_id: { type: String, required: true },
    expires_at: { type: Date, required: true },
    created_at: { type: Date, required: true, default: () => new Date() },
    ip: { type: String, default: null },
    user_agent: { type: String, default: null },
  },
  {
    collection: "sessions",
    versionKey: false,
    _id: false,
    timestamps: false,
  },
);

sessionSchema.index({ expires_at: 1 }, { expireAfterSeconds: 0 });

export type SessionDocument = HydratedDocument<SessionAttrs>;

export const Session = model<SessionAttrs>("Session", sessionSchema);
