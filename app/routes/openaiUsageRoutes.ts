import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  openaiSpendHandler,
  openaiBalanceHandler,
} from "../controllers/openaiUsageController";

const router = Router();

router.use(requireAuth);
router.get("/spend", asyncHandler(openaiSpendHandler));
router.post("/balance", asyncHandler(openaiBalanceHandler));

export default router;
