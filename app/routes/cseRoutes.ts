import { Router } from "express";
import { listCseCompanies } from "../services/cseCompanyCatalogService";

const router = Router();

router.get("/companies", async (_req, res, next) => {
  try {
    const result = await listCseCompanies();
    res.json(result);
  } catch (err) {
    next(err);
  }
});

export default router;
