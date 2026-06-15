import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  deleteReportScheduleHandler,
  getReportScheduleHandler,
  runDueSchedulesHandler,
  saveReportScheduleHandler,
} from "../controllers/reportScheduleController";

const router = Router();

router.use(requireAuth);
router.post("/run-due", asyncHandler(runDueSchedulesHandler));
router.get("/:agent", asyncHandler(getReportScheduleHandler));
router.put("/:agent", asyncHandler(saveReportScheduleHandler));
router.delete("/:agent", asyncHandler(deleteReportScheduleHandler));

export default router;
