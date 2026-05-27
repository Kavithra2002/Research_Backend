import { Types } from "mongoose";
import {
  LoginHistory,
  type LoginEvent,
} from "../models/LoginHistory";
import { User } from "../models/User";
import { HttpError } from "../utils/httpError";

export interface LoginHistoryPublic {
  _id: string;
  user_id: string;
  user_business_id: string;
  email: string;
  event: LoginEvent;
  success: boolean;
  ip: string | null;
  user_agent: string | null;
  session_id: string | null;
  logged_at: Date;
}

export interface ListHistoryOptions {
  userId?: string;
  userBusinessId?: string;
  event?: LoginEvent;
  limit?: number;
}

function toPublic(doc: Record<string, unknown>): LoginHistoryPublic {
  return {
    _id: String(doc._id),
    user_id: String(doc.user_id),
    user_business_id: String(doc.user_business_id ?? ""),
    email: String(doc.email ?? ""),
    event: (doc.event as LoginEvent) ?? "login",
    success: Boolean(doc.success),
    ip: typeof doc.ip === "string" ? doc.ip : null,
    user_agent: typeof doc.user_agent === "string" ? doc.user_agent : null,
    session_id:
      typeof doc.session_id === "string" ? doc.session_id : null,
    logged_at: doc.logged_at as Date,
  };
}

async function resolveUserObjectId(opts: ListHistoryOptions) {
  if (opts.userId) {
    if (!Types.ObjectId.isValid(opts.userId)) {
      throw HttpError.badRequest("Invalid userId");
    }
    return new Types.ObjectId(opts.userId);
  }
  if (opts.userBusinessId) {
    const u = await User.findOne({ user_id: opts.userBusinessId }).lean();
    if (!u) throw HttpError.notFound(`User ${opts.userBusinessId} not found`);
    return u._id;
  }
  return null;
}

export async function listLoginHistory(
  opts: ListHistoryOptions = {},
): Promise<{ items: LoginHistoryPublic[]; total: number }> {
  const filter: Record<string, unknown> = {};
  const oid = await resolveUserObjectId(opts);
  if (oid) filter.user_id = oid;
  if (opts.event) filter.event = opts.event;

  const limit = Math.min(500, Math.max(1, opts.limit ?? 100));

  const [items, total] = await Promise.all([
    LoginHistory.find(filter)
      .sort({ logged_at: -1 })
      .limit(limit)
      .lean(),
    LoginHistory.countDocuments(filter),
  ]);

  return {
    items: items.map((doc) => toPublic(doc as Record<string, unknown>)),
    total,
  };
}

export async function getLastLoginForUser(
  userId: string,
): Promise<LoginHistoryPublic | null> {
  if (!Types.ObjectId.isValid(userId)) {
    throw HttpError.badRequest("Invalid userId");
  }
  const doc = await LoginHistory.findOne({
    user_id: new Types.ObjectId(userId),
    event: { $in: ["login", "signup"] },
    success: true,
  })
    .sort({ logged_at: -1 })
    .lean();
  if (!doc) return null;
  return toPublic(doc as Record<string, unknown>);
}
