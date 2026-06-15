import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  tuckChatHandler,
  tuckDeleteConfigHandler,
  tuckGetConfigHandler,
  tuckReportHandler,
  tuckReportsListHandler,
  tuckSaveConfigHandler,
  tuckSectionsHandler,
} from "../controllers/tuckAgentController";

const router = Router();

router.use(requireAuth);
router.post("/chat", asyncHandler(tuckChatHandler));
router.get("/sections", asyncHandler(tuckSectionsHandler));
router.get("/config", asyncHandler(tuckGetConfigHandler));
router.put("/config", asyncHandler(tuckSaveConfigHandler));
router.delete("/config", asyncHandler(tuckDeleteConfigHandler));
router.post("/report", asyncHandler(tuckReportHandler));
router.get("/reports", asyncHandler(tuckReportsListHandler));

export default router;
