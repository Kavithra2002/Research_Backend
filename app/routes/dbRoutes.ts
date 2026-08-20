import { Router } from "express";
import {
  buildCombWorkbookExport,
  buildFsWorkbookExport,
  getDbDriversPreview,
  getDbFsPreview,
  getDbNotesPreview,
  getDbQuarterlyPreview,
  getDbRatiosPreview,
  isCombPilotAvailable,
  isQuarterlyDbAvailable,
  COMMERCIAL_BANK_SLUG,
} from "../services/newspaperDbExportService";

const router = Router();

function requireCombPilot(company: string, res: import("express").Response): boolean {
  if (!isCombPilotAvailable(company)) {
    res.status(403).json({
      error: "Missing company for this view.",
    });
    return false;
  }
  return true;
}

router.get("/preview", async (req, res, next) => {
  try {
    const view = typeof req.query.view === "string" ? req.query.view : "fs";
    const company =
      typeof req.query.company === "string" ? req.query.company.trim() : "";

    if (view === "quarterly") {
      const payload = await getDbQuarterlyPreview(
        company || COMMERCIAL_BANK_SLUG,
      );
      return res.json(payload);
    }

    if (view === "drivers" || view === "ratios") {
      if (!company) {
        return res
          .status(400)
          .json({ error: "Missing required query param: company" });
      }
      if (!requireCombPilot(company, res)) return;
      const payload =
        view === "drivers"
          ? await getDbDriversPreview(company)
          : await getDbRatiosPreview(company);
      return res.json(payload);
    }

    if (view === "notes") {
      if (!company) {
        return res
          .status(400)
          .json({ error: "Missing required query param: company" });
      }
      const payload = await getDbNotesPreview(company);
      return res.json(payload);
    }

    if (!company) {
      return res
        .status(400)
        .json({ error: "Missing required query param: company" });
    }

    const payload = await getDbFsPreview(company);
    res.json({
      ...payload,
      quarterly_available: isQuarterlyDbAvailable(company),
      comb_pilot_available: isCombPilotAvailable(company),
    });
  } catch (err) {
    next(err);
  }
});

router.get("/export", async (req, res, next) => {
  try {
    const company =
      typeof req.query.company === "string" ? req.query.company.trim() : "";
    if (!company) {
      return res
        .status(400)
        .json({ error: "Missing required query param: company" });
    }

    const result = await buildFsWorkbookExport(company);
    res.setHeader(
      "Content-Type",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    );
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${result.filename}"`,
    );
    res.setHeader("X-Cells-Filled", String(result.cellsFilled));
    res.setHeader("X-Cells-Missing", String(result.cellsMissing));
    res.send(result.buffer);
  } catch (err) {
    next(err);
  }
});

router.get("/export-comb", async (req, res, next) => {
  try {
    const company =
      typeof req.query.company === "string" ? req.query.company.trim() : "";
    if (!company) {
      return res
        .status(400)
        .json({ error: "Missing required query param: company" });
    }
    if (!isCombPilotAvailable(company)) {
      return res.status(403).json({
        error: "COMB workbook export is only available for Commercial Bank of Ceylon PLC.",
      });
    }

    const result = await buildCombWorkbookExport(company);
    res.setHeader(
      "Content-Type",
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    );
    res.setHeader(
      "Content-Disposition",
      `attachment; filename="${result.filename}"`,
    );
    res.setHeader("X-Cells-Filled", String(result.cellsFilled));
    res.setHeader("X-Cells-Missing", String(result.cellsMissing));
    res.send(result.buffer);
  } catch (err) {
    next(err);
  }
});

export default router;
