import {
  Schema,
  model,
  type HydratedDocument,
  type Types,
} from "mongoose";

export const LOGIN_EVENTS = [
  "login",
  "signup",
  "logout",
  "password_reset_request",
  "password_reset_success",
] as const;
export type LoginEvent = (typeof LOGIN_EVENTS)[number];

export interface LoginHistoryAttrs {
  user_id: Types.ObjectId;
  user_business_id: string;
  email: string;
  event: LoginEvent;
  success: boolean;
  ip: string | null;
  user_agent: string | null;
  session_id: string | null;
  logged_at: Date;
}

const loginHistorySchema = new Schema<LoginHistoryAttrs>(
  {
    user_id: {
      type: Schema.Types.ObjectId,
      ref: "User",
      required: true,
      index: true,
    },
    user_business_id: {
      type: String,
      required: true,
      index: true,
    },
    email: {
      type: String,
      required: true,
      lowercase: true,
      trim: true,
    },
    event: {
      type: String,
      enum: LOGIN_EVENTS,
      default: "login",
      required: true,
      index: true,
    },
    success: {
      type: Boolean,
      default: true,
      required: true,
    },
    ip: { type: String, default: null },
    user_agent: { type: String, default: null },
    session_id: { type: String, default: null },
    logged_at: {
      type: Date,
      required: true,
      default: () => new Date(),
      index: true,
    },
  },
  {
    collection: "login_history",
    versionKey: false,
    timestamps: false,
    toJSON: {
      virtuals: false,
      transform: (_doc, ret) => {
        const out = ret as Record<string, unknown> & { _id?: unknown };
        if (out._id !== undefined) out._id = String(out._id);
        if (out.user_id !== undefined) out.user_id = String(out.user_id);
        return out;
      },
    },
  },
);

export type LoginHistoryDocument = HydratedDocument<LoginHistoryAttrs>;

export const LoginHistory = model<LoginHistoryAttrs>(
  "LoginHistory",
  loginHistorySchema,
);
