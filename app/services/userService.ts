import { User, type UserAttrs, type UserRole, type UserStatus } from "../models/User";
import { HttpError } from "../utils/httpError";
import { hashPassword } from "../utils/password";

export interface CreateUserInput {
  user_id?: string;
  first_name: string;
  last_name: string;
  email: string;
  password: string;
  user_status?: UserStatus;
  role?: UserRole;
}

export interface UpdateUserInput {
  first_name?: string;
  last_name?: string;
  email?: string;
  password?: string;
  user_status?: UserStatus;
  role?: UserRole;
  last_login?: Date | null;
}

export interface ListUsersOptions {
  page?: number;
  limit?: number;
  status?: string;
  search?: string;
}

export type UserPublic = Omit<UserAttrs, "password"> & { _id: string };

async function generateNextUserId(): Promise<string> {
  const last = await User.findOne({ user_id: /^USR_\d+$/ })
    .sort({ user_id: -1 })
    .lean();

  const lastId = last?.user_id;
  if (!lastId) return "USR_001";
  const match = /^USR_(\d+)$/.exec(lastId);
  if (!match) return "USR_001";
  const next = Number(match[1]) + 1;
  return `USR_${String(next).padStart(3, "0")}`;
}

export async function listUsers(opts: ListUsersOptions = {}) {
  const page = Math.max(1, opts.page ?? 1);
  const limit = Math.min(100, Math.max(1, opts.limit ?? 25));
  const filter: Record<string, unknown> = {};

  if (opts.status) filter.user_status = opts.status;
  if (opts.search && opts.search.trim().length > 0) {
    const regex = new RegExp(escapeRegex(opts.search.trim()), "i");
    filter.$or = [
      { user_id: regex },
      { first_name: regex },
      { last_name: regex },
      { email: regex },
    ];
  }

  const [items, total] = await Promise.all([
    User.find(filter)
      .sort({ created_at: -1 })
      .skip((page - 1) * limit)
      .limit(limit)
      .lean(),
    User.countDocuments(filter),
  ]);

  return {
    items: items.map(toPublicUser),
    page,
    limit,
    total,
    totalPages: Math.max(1, Math.ceil(total / limit)),
  };
}

export async function getUserById(id: string): Promise<UserPublic> {
  const user = await User.findById(id).lean();
  if (!user) throw HttpError.notFound(`User ${id} not found`);
  return toPublicUser(user);
}

export async function getUserByUserId(userId: string): Promise<UserPublic> {
  const user = await User.findOne({ user_id: userId }).lean();
  if (!user) throw HttpError.notFound(`User ${userId} not found`);
  return toPublicUser(user);
}

export async function createUser(input: CreateUserInput): Promise<UserPublic> {
  const normalizedEmail = input.email.trim().toLowerCase();
  const existing = await User.findOne({ email: normalizedEmail }).lean();
  if (existing) {
    throw HttpError.conflict(`Email ${normalizedEmail} is already in use`);
  }

  const user_id = input.user_id?.trim() || (await generateNextUserId());
  const hashedPassword = await hashPassword(input.password);

  const created = await User.create({
    user_id,
    first_name: input.first_name.trim(),
    last_name: input.last_name.trim(),
    email: normalizedEmail,
    password: hashedPassword,
    user_status: input.user_status ?? "active",
    role: input.role ?? "User",
    last_login: null,
  });

  return toPublicUser(created.toObject() as unknown as Record<string, unknown>);
}

export async function updateUser(
  id: string,
  input: UpdateUserInput,
): Promise<UserPublic> {
  const patch: UpdateUserInput = { ...input };
  if (input.email) {
    patch.email = input.email.trim().toLowerCase();
    const conflict = await User.findOne({
      email: patch.email,
      _id: { $ne: id },
    }).lean();
    if (conflict) {
      throw HttpError.conflict(`Email ${patch.email} is already in use`);
    }
  }
  if (input.password) {
    patch.password = await hashPassword(input.password);
  }

  const updated = await User.findByIdAndUpdate(id, patch, {
    new: true,
    runValidators: true,
  }).lean();

  if (!updated) throw HttpError.notFound(`User ${id} not found`);
  return toPublicUser(updated);
}

export async function deleteUser(id: string): Promise<UserPublic> {
  const deleted = await User.findByIdAndDelete(id).lean();
  if (!deleted) throw HttpError.notFound(`User ${id} not found`);
  return toPublicUser(deleted);
}

export async function recordLogin(id: string): Promise<UserPublic> {
  const updated = await User.findByIdAndUpdate(
    id,
    { last_login: new Date() },
    { new: true },
  ).lean();
  if (!updated) throw HttpError.notFound(`User ${id} not found`);
  return toPublicUser(updated);
}

function toPublicUser(doc: Record<string, unknown>): UserPublic {
  const {
    password: _password,
    _id,
    user_id,
    first_name,
    last_name,
    email,
    user_status,
    role,
    last_login,
    created_at,
    updated_at,
  } = doc as Record<string, unknown>;
  void _password;
  const safeRole: UserRole =
    role === "Admin" || role === "User" ? role : "User";
  return {
    _id: String(_id),
    user_id: String(user_id ?? ""),
    first_name: String(first_name ?? ""),
    last_name: String(last_name ?? ""),
    email: String(email ?? ""),
    user_status: (user_status as UserStatus) ?? "active",
    role: safeRole,
    last_login: (last_login as Date | null) ?? null,
    created_at: created_at as Date,
    updated_at: updated_at as Date,
  };
}

function escapeRegex(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
