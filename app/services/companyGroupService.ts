import { Types } from "mongoose";
import {
  CompanyGroup,
  type CompanyGroupAttrs,
} from "../models/CompanyGroup";
import { HttpError } from "../utils/httpError";

export interface CompanyGroupPublic {
  _id: string;
  name: string;
  description: string;
  companies: string[];
  symbols: string[];
  created_by: string | null;
  created_by_user_id: string | null;
  created_at: Date;
  updated_at: Date;
}

export interface CompanyGroupInput {
  name: string;
  description?: string;
  companies?: string[];
  symbols?: string[];
}

export interface OwnerMeta {
  userObjectId?: Types.ObjectId | string | null;
  userBusinessId?: string | null;
}

function toPublic(doc: Record<string, unknown>): CompanyGroupPublic {
  return {
    _id: String(doc._id),
    name: String(doc.name ?? ""),
    description: String(doc.description ?? ""),
    companies: Array.isArray(doc.companies)
      ? (doc.companies as unknown[]).map((c) => String(c))
      : [],
    symbols: Array.isArray(doc.symbols)
      ? (doc.symbols as unknown[]).map((c) => String(c))
      : [],
    created_by:
      doc.created_by !== null && doc.created_by !== undefined
        ? String(doc.created_by)
        : null,
    created_by_user_id:
      typeof doc.created_by_user_id === "string"
        ? doc.created_by_user_id
        : null,
    created_at: doc.created_at as Date,
    updated_at: doc.updated_at as Date,
  };
}

function uniqueClean(values?: string[]): string[] {
  if (!Array.isArray(values)) return [];
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of values) {
    if (typeof v !== "string") continue;
    const trimmed = v.trim();
    if (!trimmed) continue;
    const key = trimmed.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(trimmed);
  }
  return out;
}

function normalizeInput(input: CompanyGroupInput): {
  name: string;
  description: string;
  companies: string[];
  symbols: string[];
} {
  return {
    name: (input.name ?? "").trim(),
    description: (input.description ?? "").trim(),
    companies: uniqueClean(input.companies),
    symbols: uniqueClean(input.symbols).map((s) => s.toUpperCase()),
  };
}

function resolveOwner(meta?: OwnerMeta) {
  const rawId = meta?.userObjectId;
  let objectId: Types.ObjectId | null = null;
  if (rawId) {
    if (rawId instanceof Types.ObjectId) {
      objectId = rawId;
    } else if (typeof rawId === "string" && Types.ObjectId.isValid(rawId)) {
      objectId = new Types.ObjectId(rawId);
    }
  }
  const businessId = meta?.userBusinessId ?? null;
  return { objectId, businessId };
}

export async function listGroups(): Promise<CompanyGroupPublic[]> {
  const items = await CompanyGroup.find({}).sort({ name: 1 }).lean();
  return items.map((doc) => toPublic(doc as Record<string, unknown>));
}

export async function getGroup(
  id: string,
): Promise<CompanyGroupPublic | null> {
  if (!Types.ObjectId.isValid(id)) return null;
  const doc = await CompanyGroup.findById(id).lean();
  if (!doc) return null;
  return toPublic(doc as Record<string, unknown>);
}

export async function createGroup(
  input: CompanyGroupInput,
  meta?: OwnerMeta,
): Promise<CompanyGroupPublic> {
  const normalized = normalizeInput(input);
  if (!normalized.name) {
    throw HttpError.badRequest("Group name is required");
  }

  const existing = await CompanyGroup.findOne({ name: normalized.name });
  if (existing) {
    throw HttpError.badRequest(
      `A group named "${normalized.name}" already exists`,
    );
  }

  const { objectId, businessId } = resolveOwner(meta);
  const created = await CompanyGroup.create({
    name: normalized.name,
    description: normalized.description,
    companies: normalized.companies,
    symbols: normalized.symbols,
    created_by: objectId,
    created_by_user_id: businessId,
  } as CompanyGroupAttrs);

  return toPublic(created.toJSON() as Record<string, unknown>);
}

export async function updateGroup(
  id: string,
  input: CompanyGroupInput,
): Promise<CompanyGroupPublic> {
  if (!Types.ObjectId.isValid(id)) {
    throw HttpError.badRequest("Invalid group id");
  }

  const normalized = normalizeInput(input);
  if (!normalized.name) {
    throw HttpError.badRequest("Group name is required");
  }

  const conflict = await CompanyGroup.findOne({
    name: normalized.name,
    _id: { $ne: new Types.ObjectId(id) },
  });
  if (conflict) {
    throw HttpError.badRequest(
      `A group named "${normalized.name}" already exists`,
    );
  }

  const updated = await CompanyGroup.findByIdAndUpdate(
    id,
    {
      $set: {
        name: normalized.name,
        description: normalized.description,
        companies: normalized.companies,
        symbols: normalized.symbols,
      },
    },
    { new: true },
  ).lean();

  if (!updated) {
    throw HttpError.notFound("Group not found");
  }

  return toPublic(updated as Record<string, unknown>);
}

export async function deleteGroup(id: string): Promise<{ removed: boolean }> {
  if (!Types.ObjectId.isValid(id)) return { removed: false };
  const res = await CompanyGroup.deleteOne({ _id: new Types.ObjectId(id) });
  return { removed: (res.deletedCount ?? 0) > 0 };
}

export type { CompanyGroupAttrs };
