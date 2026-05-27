import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  createUser,
  deleteUser,
  getUserById,
  getUserByUserId,
  listUsers,
  recordLogin,
  updateUser,
} from "../services/userService";
import { USER_ROLES, USER_STATUSES } from "../models/User";

const userStatusSchema = z.enum(USER_STATUSES);
const userRoleSchema = z.enum(USER_ROLES);

const createUserSchema = z.object({
  user_id: z
    .string()
    .trim()
    .regex(/^USR_\d+$/, "user_id must look like USR_001")
    .optional(),
  first_name: z.string().trim().min(1),
  last_name: z.string().trim().min(1),
  email: z.string().trim().email(),
  password: z.string().min(6),
  user_status: userStatusSchema.optional(),
  role: userRoleSchema.optional(),
});

const updateUserSchema = z
  .object({
    first_name: z.string().trim().min(1).optional(),
    last_name: z.string().trim().min(1).optional(),
    email: z.string().trim().email().optional(),
    password: z.string().min(6).optional(),
    user_status: userStatusSchema.optional(),
    role: userRoleSchema.optional(),
    last_login: z
      .union([z.string().datetime(), z.null()])
      .optional()
      .transform((v) => (typeof v === "string" ? new Date(v) : v)),
  })
  .refine((obj) => Object.keys(obj).length > 0, {
    message: "Provide at least one field to update",
  });

const listQuerySchema = z.object({
  page: z.coerce.number().int().positive().optional(),
  limit: z.coerce.number().int().positive().max(100).optional(),
  status: userStatusSchema.optional(),
  search: z.string().trim().min(1).optional(),
});

function unwrap<T>(parsed: z.SafeParseReturnType<unknown, T>): T {
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }
  return parsed.data;
}

export async function listUsersHandler(req: Request, res: Response) {
  const query = unwrap(listQuerySchema.safeParse(req.query));
  const result = await listUsers(query);
  res.json(result);
}

export async function getUserHandler(req: Request, res: Response) {
  const { id } = req.params;
  const isObjectId = /^[a-f0-9]{24}$/i.test(id);
  const user = isObjectId
    ? await getUserById(id)
    : await getUserByUserId(id);
  res.json(user);
}

export async function createUserHandler(req: Request, res: Response) {
  const body = unwrap(createUserSchema.safeParse(req.body));
  const created = await createUser(body);
  res.status(201).json(created);
}

export async function updateUserHandler(req: Request, res: Response) {
  const { id } = req.params;
  const body = unwrap(updateUserSchema.safeParse(req.body));
  const updated = await updateUser(id, body);
  res.json(updated);
}

export async function deleteUserHandler(req: Request, res: Response) {
  const { id } = req.params;
  const deleted = await deleteUser(id);
  res.json(deleted);
}

export async function recordLoginHandler(req: Request, res: Response) {
  const { id } = req.params;
  const updated = await recordLogin(id);
  res.json(updated);
}
