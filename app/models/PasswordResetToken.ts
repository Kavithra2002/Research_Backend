import {
  Schema,
  model,
  type HydratedDocument,
  type Types,
} from "mongoose";

export interface PasswordResetTokenAttrs {
  user_id: Types.ObjectId;
  token_hash: string;
  expires_at: Date;
  used_at: Date | null;
  ip: string | null;
  user_agent: string | null;
  created_at: Date;
}

const passwordResetTokenSchema = new Schema<PasswordResetTokenAttrs>(
  {
    user_id: {
      type: Schema.Types.ObjectId,
      ref: "User",
      required: true,
      index: true,
    },
    token_hash: {
      type: String,
      required: true,
      index: true,
    },
    expires_at: {
      type: Date,
      required: true,
      index: true,
    },
    used_at: {
      type: Date,
      default: null,
    },
    ip: { type: String, default: null },
    user_agent: { type: String, default: null },
    created_at: {
      type: Date,
      required: true,
      default: () => new Date(),
    },
  },
  {
    collection: "password_reset_tokens",
    versionKey: false,
    timestamps: false,
  },
);

passwordResetTokenSchema.index({ expires_at: 1 }, { expireAfterSeconds: 0 });

export type PasswordResetTokenDocument =
  HydratedDocument<PasswordResetTokenAttrs>;

export const PasswordResetToken = model<PasswordResetTokenAttrs>(
  "PasswordResetToken",
  passwordResetTokenSchema,
);
