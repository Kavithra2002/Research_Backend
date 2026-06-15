import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import { requireRole } from "../middleware/requireRole";
import {
  ragReindexHandler,
  ragSearchHandler,
} from "../controllers/ragController";

const router = Router();

router.use(requireAuth);

// Building the index calls the OpenAI embeddings API and writes the store —
// restrict it to admins.
router.post("/reindex", requireRole("Admin"), asyncHandler(ragReindexHandler));

// Searching is read-only; any signed-in user may use it.
router.post("/search", asyncHandler(ragSearchHandler));

export default router;
