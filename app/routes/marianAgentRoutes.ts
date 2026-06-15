import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  marianChatHandler,
  marianDataHandler,
  marianDeleteConfigHandler,
  marianGetConfigHandler,
  marianReportHandler,
  marianReportPdfHandler,
  marianReportsListHandler,
  marianSaveConfigHandler,
  marianSectionsHandler,
} from "../controllers/marianAgentController";

const router = Router();

router.use(requireAuth);
router.post("/chat", asyncHandler(marianChatHandler));
router.get("/sections", asyncHandler(marianSectionsHandler));
router.get("/config", asyncHandler(marianGetConfigHandler));
router.put("/config", asyncHandler(marianSaveConfigHandler));
router.delete("/config", asyncHandler(marianDeleteConfigHandler));
router.post("/report", asyncHandler(marianReportHandler));
router.post("/report-pdf", asyncHandler(marianReportPdfHandler));
router.post("/data", asyncHandler(marianDataHandler));
router.get("/reports", asyncHandler(marianReportsListHandler));

export default router;
