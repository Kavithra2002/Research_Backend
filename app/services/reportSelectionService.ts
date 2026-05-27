import { Types } from "mongoose";
import {
  ReportSelection,
  type ReportSelectionAttrs,
} from "../models/ReportSelection";

export interface ReportSelectionPublic {
  _id: string;
  company: string;
  report_type: string;
  file_name: string;
  selected_by: string | null;
  selected_by_user_id: string | null;
  created_at: Date;
  updated_at: Date;
}

export interface SelectionItem {
  company: string;
  report_type: string;
  file_name: string;
}

export interface SelectorMeta {
  userObjectId?: Types.ObjectId | string | null;
  userBusinessId?: string | null;
}

function toPublic(doc: Record<string, unknown>): ReportSelectionPublic {
  return {
    _id: String(doc._id),
    company: String(doc.company ?? ""),
    report_type: String(doc.report_type ?? ""),
    file_name: String(doc.file_name ?? ""),
    selected_by:
      doc.selected_by !== null && doc.selected_by !== undefined
        ? String(doc.selected_by)
        : null,
    selected_by_user_id:
      typeof doc.selected_by_user_id === "string"
        ? doc.selected_by_user_id
        : null,
    created_at: doc.created_at as Date,
    updated_at: doc.updated_at as Date,
  };
}

function normalizeItem(item: SelectionItem): SelectionItem {
  return {
    company: item.company.trim(),
    report_type: item.report_type.trim(),
    file_name: item.file_name.trim(),
  };
}

function resolveOwner(meta?: SelectorMeta) {
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

export async function listSelections(): Promise<ReportSelectionPublic[]> {
  const items = await ReportSelection.find({})
    .sort({ company: 1, report_type: 1, file_name: 1 })
    .lean();
  return items.map((doc) => toPublic(doc as Record<string, unknown>));
}

export async function addSelection(
  item: SelectionItem,
  meta?: SelectorMeta,
): Promise<ReportSelectionPublic> {
  const normalized = normalizeItem(item);
  if (!normalized.company || !normalized.report_type || !normalized.file_name) {
    throw new Error("company, report_type and file_name are required");
  }

  const { objectId, businessId } = resolveOwner(meta);
  const now = new Date();

  const updated = await ReportSelection.findOneAndUpdate(
    {
      company: normalized.company,
      report_type: normalized.report_type,
      file_name: normalized.file_name,
    },
    {
      $setOnInsert: {
        company: normalized.company,
        report_type: normalized.report_type,
        file_name: normalized.file_name,
        created_at: now,
      },
      $set: {
        selected_by: objectId,
        selected_by_user_id: businessId,
        updated_at: now,
      },
    },
    { upsert: true, new: true, setDefaultsOnInsert: true },
  ).lean();

  return toPublic(updated as Record<string, unknown>);
}

export async function removeSelection(
  item: SelectionItem,
): Promise<{ removed: boolean }> {
  const normalized = normalizeItem(item);
  const res = await ReportSelection.deleteOne({
    company: normalized.company,
    report_type: normalized.report_type,
    file_name: normalized.file_name,
  });
  return { removed: (res.deletedCount ?? 0) > 0 };
}

export async function replaceSelections(
  items: SelectionItem[],
  meta?: SelectorMeta,
): Promise<{
  selections: ReportSelectionPublic[];
  added: number;
  removed: number;
}> {
  const normalized = items
    .map(normalizeItem)
    .filter((i) => i.company && i.report_type && i.file_name);

  const { objectId, businessId } = resolveOwner(meta);
  const now = new Date();

  if (normalized.length === 0) {
    const del = await ReportSelection.deleteMany({});
    return {
      selections: [],
      added: 0,
      removed: del.deletedCount ?? 0,
    };
  }

  const ops = normalized.map((item) => ({
    updateOne: {
      filter: {
        company: item.company,
        report_type: item.report_type,
        file_name: item.file_name,
      },
      update: {
        $setOnInsert: {
          company: item.company,
          report_type: item.report_type,
          file_name: item.file_name,
          created_at: now,
        },
        $set: {
          selected_by: objectId,
          selected_by_user_id: businessId,
          updated_at: now,
        },
      },
      upsert: true,
    },
  }));

  const writeResult = await ReportSelection.bulkWrite(ops, {
    ordered: false,
  });
  const upserted = writeResult.upsertedCount ?? 0;

  const keep = normalized.map((item) => ({
    company: item.company,
    report_type: item.report_type,
    file_name: item.file_name,
  }));
  const del = await ReportSelection.deleteMany({
    $nor: keep.map((k) => ({
      company: k.company,
      report_type: k.report_type,
      file_name: k.file_name,
    })),
  });

  const selections = await listSelections();
  return {
    selections,
    added: upserted,
    removed: del.deletedCount ?? 0,
  };
}

export async function clearSelections(): Promise<{ removed: number }> {
  const res = await ReportSelection.deleteMany({});
  return { removed: res.deletedCount ?? 0 };
}

export type { ReportSelectionAttrs };
