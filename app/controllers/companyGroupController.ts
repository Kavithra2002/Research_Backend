import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  createGroup,
  deleteGroup,
  getGroup,
  listGroups,
  updateGroup,
  type OwnerMeta,
} from "../services/companyGroupService";

const groupInputSchema = z.object({
  name: z.string().trim().min(1, "Group name is required"),
  description: z.string().trim().optional().default(""),
  companies: z.array(z.string().trim().min(1)).optional().default([]),
  symbols: z.array(z.string().trim().min(1)).optional().default([]),
});

function unwrap<T>(parsed: z.SafeParseReturnType<unknown, T>): T {
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  return parsed.data;
}

function readMeta(req: Request): OwnerMeta {
  const user = req.user;
  return {
    userObjectId: user?._id ?? null,
    userBusinessId: user?.user_id ?? null,
  };
}

export async function listGroupsHandler(_req: Request, res: Response) {
  const groups = await listGroups();
  res.json({ groups });
}

export async function getGroupHandler(req: Request, res: Response) {
  const group = await getGroup(req.params.id);
  if (!group) {
    throw HttpError.notFound("Group not found");
  }
  res.json(group);
}

export async function createGroupHandler(req: Request, res: Response) {
  const body = unwrap(groupInputSchema.safeParse(req.body));
  const created = await createGroup(body, readMeta(req));
  res.status(201).json(created);
}

export async function updateGroupHandler(req: Request, res: Response) {
  const body = unwrap(groupInputSchema.safeParse(req.body));
  const updated = await updateGroup(req.params.id, body);
  res.json(updated);
}

export async function deleteGroupHandler(req: Request, res: Response) {
  const result = await deleteGroup(req.params.id);
  res.json(result);
}
