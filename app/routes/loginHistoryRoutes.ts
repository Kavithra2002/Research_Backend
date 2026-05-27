import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import { requireRole } from "../middleware/requireRole";
import {
  lastLoginHandler,
  listLoginHistoryHandler,
} from "../controllers/loginHistoryController";

const router = Router();

router.use(requireAuth, requireRole("Admin"));

router.get("/", asyncHandler(listLoginHistoryHandler));
router.get("/users/:id/last", asyncHandler(lastLoginHandler));

export default router;
