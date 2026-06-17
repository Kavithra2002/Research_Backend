import { Router } from "express";
import {
  forceRefreshNewspaper,
  getNewspaperPayload,
} from "../services/newspaperService";

const router = Router();

router.get("/", async (req, res, next) => {
  try {
    const category =
      typeof req.query.category === "string" ? req.query.category : undefined;
    const payload = await getNewspaperPayload(category);
    res.json({ source: "automated-rss", ...payload });
  } catch (err) {
    next(err);
  }
});

router.post("/refresh", async (_req, res, next) => {
  try {
    const result = await forceRefreshNewspaper();
    const payload = await getNewspaperPayload();
    res.json({ source: "automated-rss", refresh: result, ...payload });
  } catch (err) {
    next(err);
  }
});

export default router;
