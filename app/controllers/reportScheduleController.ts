import type { Request, Response } from "express";
import { z } from "zod";
import { HttpError } from "../utils/httpError";
import {
  deleteReportSchedule,
  getReportSchedule,
  runDueSchedules,
  saveReportSchedule,
} from "../services/reportScheduleService";
import type { ReportAgent } from "../models/ReportSchedule";

const agentSchema = z.enum(["robin", "tuck", "marian"]);

function parseAgent(value: unknown): ReportAgent {
  const parsed = agentSchema.safeParse(value);
  if (!parsed.success) throw HttpError.badRequest("Unknown agent");
  return parsed.data;
}

export async function getReportScheduleHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const agent = parseAgent(req.params.agent);
  const schedule = await getReportSchedule(user.user_id ?? "", agent);
  res.json({ schedule });
}

const saveSchema = z.object({
  time: z.string().default("09:00"),
  date: z.string().default(""),
  frequency: z.enum(["once", "daily", "weekly", "monthly"]).default("once"),
  emails: z.array(z.string()).default([]),
  config: z.record(z.string(), z.unknown()).default({}),
  enabled: z.boolean().default(true),
});

export async function saveReportScheduleHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const agent = parseAgent(req.params.agent);

  const parsed = saveSchema.safeParse(req.body);
  if (!parsed.success) {
    throw HttpError.badRequest("Validation failed", parsed.error.flatten());
  }

  const schedule = await saveReportSchedule(user.user_id ?? "", agent, parsed.data);
  res.json({ schedule });
}

export async function deleteReportScheduleHandler(req: Request, res: Response) {
  const user = req.user;
  if (!user) throw HttpError.badRequest("Missing authenticated user");
  const agent = parseAgent(req.params.agent);
  await deleteReportSchedule(user.user_id ?? "", agent);
  res.json({ ok: true });
}

/** Manually trigger any due schedules — handy for testing the pipeline. */
export async function runDueSchedulesHandler(_req: Request, res: Response) {
  const ran = await runDueSchedules();
  res.json({ ran });
}
