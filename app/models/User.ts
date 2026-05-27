import { Schema, model, type HydratedDocument, type Types } from "mongoose";

export const USER_STATUSES = ["active", "inactive", "suspended"] as const;
export type UserStatus = (typeof USER_STATUSES)[number];

export const USER_ROLES = ["Admin", "User"] as const;
export type UserRole = (typeof USER_ROLES)[number];

export interface UserAttrs {
  user_id: string;
  first_name: string;
  last_name: string;
  email: string;
  password: string;
  user_status: UserStatus;
  role: UserRole;
  last_login: Date | null;
  created_at: Date;
  updated_at: Date;
}

export interface UserRaw extends UserAttrs {
  _id: Types.ObjectId;
}

const userSchema = new Schema<UserAttrs>(
  {
    user_id: {
      type: String,
      required: true,
      unique: true,
      trim: true,
      index: true,
    },
    first_name: {
      type: String,
      required: true,
      trim: true,
    },
    last_name: {
      type: String,
      required: true,
      trim: true,
    },
    email: {
      type: String,
      required: true,
      unique: true,
      lowercase: true,
      trim: true,
      match: [/^[^\s@]+@[^\s@]+\.[^\s@]+$/, "Invalid email format"],
      index: true,
    },
    password: {
      type: String,
      required: true,
      minlength: 6,
    },
    user_status: {
      type: String,
      enum: USER_STATUSES,
      default: "active",
      required: true,
    },
    role: {
      type: String,
      enum: USER_ROLES,
      default: "User",
      required: true,
      index: true,
    },
    last_login: {
      type: Date,
      default: null,
    },
  },
  {
    collection: "users",
    timestamps: { createdAt: "created_at", updatedAt: "updated_at" },
    versionKey: false,
    toJSON: {
      virtuals: false,
      transform: (_doc, ret) => {
        const out = ret as Record<string, unknown> & { _id?: unknown };
        if (out._id !== undefined) {
          out._id = String(out._id);
        }
        delete (out as { password?: unknown }).password;
        return out;
      },
    },
  },
);

export type UserDocument = HydratedDocument<UserAttrs>;

export const User = model<UserAttrs>("User", userSchema);
