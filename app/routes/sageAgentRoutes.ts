import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import { sageChatHandler } from "../controllers/sageAgentController";

const router = Router();

router.use(requireAuth);
router.post("/chat", asyncHandler(sageChatHandler));

export default router;
