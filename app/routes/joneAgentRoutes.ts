import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  joneChatHandler,
  joneColumnsHandler,
  joneDeleteConfigHandler,
  joneGetConfigHandler,
  joneGroupsHandler,
  joneReportCsvHandler,
  joneReportPdfHandler,
  joneSaveConfigHandler,
} from "../controllers/joneAgentController";

const router = Router();

router.use(requireAuth);
router.post("/chat", asyncHandler(joneChatHandler));
router.get("/columns", asyncHandler(joneColumnsHandler));
router.get("/groups", asyncHandler(joneGroupsHandler));
router.get("/config", asyncHandler(joneGetConfigHandler));
router.put("/config", asyncHandler(joneSaveConfigHandler));
router.delete("/config", asyncHandler(joneDeleteConfigHandler));
router.post("/report-pdf", asyncHandler(joneReportPdfHandler));
router.post("/report-csv", asyncHandler(joneReportCsvHandler));

export default router;
