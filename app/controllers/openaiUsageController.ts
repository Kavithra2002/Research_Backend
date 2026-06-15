import type { Request, Response } from "express";
import {
  getOpenAiSpend,
  setOpenAiBalance,
} from "../services/openaiUsageService";
import { HttpError } from "../utils/httpError";

export async function openaiSpendHandler(req: Request, res: Response) {
  const force = req.query.refresh === "1" || req.query.refresh === "true";
  const spend = await getOpenAiSpend(force);
  res.json(spend);
}

export async function openaiBalanceHandler(req: Request, res: Response) {
  if (req.user?.role !== "Admin") {
    throw HttpError.forbidden("Only admins can update the credit balance.");
  }

  const body = (req.body ?? {}) as { balance?: unknown; asOf?: unknown };
  const balance = Number(body.balance);
  if (!Number.isFinite(balance)) {
    throw HttpError.badRequest("A numeric `balance` is required.");
  }
  const asOf = typeof body.asOf === "string" ? body.asOf : null;

  const spend = await setOpenAiBalance({
    balance,
    asOf,
    updatedBy: req.user?.user_id ?? null,
  });
  res.json(spend);
}
