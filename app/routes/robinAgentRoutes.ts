import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  robinChatHandler,
  robinCompaniesHandler,
  robinReportHandler,
} from "../controllers/robinAgentController";

const router = Router();

router.use(requireAuth);
router.post("/chat", asyncHandler(robinChatHandler));
router.get("/companies", asyncHandler(robinCompaniesHandler));
router.post("/report", asyncHandler(robinReportHandler));

export default router;
