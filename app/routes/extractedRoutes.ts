import { Router } from "express";
import {
  countFinancialTables,
  getExtractedResults,
  getReportTables,
  getSectorLens,
  getSectorLensLive,
  listExtractedCompanies,
  listStoredReports,
  type PeriodLabel,
} from "../services/extractedService";
import {
  countNonFinancialData,
  getNonFinancialData,
  listNonFinancialCompanies,
} from "../services/nonFinancialService";

const router = Router();

function normalizePeriod(raw: unknown): PeriodLabel {
  const t = String(raw ?? "")
    .trim()
    .toLowerCase();
  if (
    t === "quarterly" ||
    t === "quarter" ||
    t === "interim" ||
    t === "q"
  ) {
    return "Quarterly";
  }
  return "Annual";
}

router.get("/", async (_req, res, next) => {
  try {
    const tableCount = await countFinancialTables();
    if (tableCount === 0) {
      return res.json({ source: "mongodb", companies: [] });
    }
    const companies = await listExtractedCompanies();
    res.json({ source: "mongodb", companies });
  } catch (err) {
    next(err);
  }
});

router.get("/sector-lens", async (req, res, next) => {
  try {
    const asOf = typeof req.query.asOf === "string" ? req.query.asOf : undefined;
    const payload = await getSectorLens(asOf);
    res.json({ source: "mongodb", ...payload });
  } catch (err) {
    next(err);
  }
});

router.get("/sector-lens-live", async (_req, res, next) => {
  try {
    const payload = await getSectorLensLive();
    res.json({ source: "cse", ...payload });
  } catch (err) {
    next(err);
  }
});

router.get("/stored-reports", async (_req, res, next) => {
  try {
    const tableCount = await countFinancialTables();
    if (tableCount === 0) {
      return res.json({ source: "mongodb", companies: [] });
    }
    const companies = await listStoredReports();
    res.json({ source: "mongodb", companies });
  } catch (err) {
    next(err);
  }
});

router.get("/report-data", async (req, res, next) => {
  try {
    const company = String(req.query.company ?? "").trim();
    const reportKey = String(req.query.reportKey ?? "").trim();
    const reportType =
      normalizePeriod(req.query.reportType ?? req.query.period) === "Quarterly"
        ? "quarterly"
        : "annual";

    if (!company || !reportKey) {
      return res.status(400).json({
        error: "Missing required query param(s): company, reportKey",
      });
    }

    const payload = await getReportTables(company, reportType, reportKey);
    res.json(payload);
  } catch (err) {
    const status =
      err && typeof err === "object" && "statusCode" in err
        ? Number((err as { statusCode: number }).statusCode)
        : 500;
    if (status === 404) {
      return res.status(404).json({
        error: "Stored report not found",
        company: String(req.query.company ?? ""),
        reportKey: String(req.query.reportKey ?? ""),
      });
    }
    next(err);
  }
});

router.get("/non-financial", async (_req, res, next) => {
  try {
    const count = await countNonFinancialData();
    if (count === 0) {
      return res.json({ source: "mongodb", companies: [] });
    }
    const companies = await listNonFinancialCompanies();
    res.json({ source: "mongodb", companies });
  } catch (err) {
    next(err);
  }
});

router.get("/non-financial-data", async (req, res, next) => {
  try {
    const company = String(req.query.company ?? "").trim();
    const reportKey = String(req.query.reportKey ?? "").trim();

    if (!company || !reportKey) {
      return res.status(400).json({
        error: "Missing required query param(s): company, reportKey",
      });
    }

    const payload = await getNonFinancialData(company, reportKey);
    res.json(payload);
  } catch (err) {
    const status =
      err && typeof err === "object" && "statusCode" in err
        ? Number((err as { statusCode: number }).statusCode)
        : 500;
    if (status === 404) {
      return res.status(404).json({
        error: "Non-financial data not found",
        company: String(req.query.company ?? ""),
        reportKey: String(req.query.reportKey ?? ""),
      });
    }
    next(err);
  }
});

router.get("/data", async (req, res, next) => {
  try {
    const company = String(req.query.company ?? "").trim();
    const yearRaw = req.query.year;
    const period = normalizePeriod(req.query.period);

    if (!company) {
      return res.status(400).json({ error: "Missing required query param: company" });
    }

    const year = Number(yearRaw);
    if (!Number.isFinite(year)) {
      return res.status(400).json({
        error: "Missing or invalid query param: year",
        company,
        period,
      });
    }

    const payload = await getExtractedResults(company, year, period);
    res.json(payload);
  } catch (err) {
    const status =
      err && typeof err === "object" && "statusCode" in err
        ? Number((err as { statusCode: number }).statusCode)
        : 500;
    if (status === 404) {
      return res.status(404).json({
        error: "Extracted results not found",
        company: String(req.query.company ?? ""),
        year: req.query.year,
        period: normalizePeriod(req.query.period),
      });
    }
    next(err);
  }
});

export default router;
