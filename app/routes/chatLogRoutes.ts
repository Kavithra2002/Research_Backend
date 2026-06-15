import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  deleteChatSessionHandler,
  getChatSessionHandler,
  listChatSessionsHandler,
  saveChatSessionHandler,
} from "../controllers/chatLogController";

const router = Router();

router.use(requireAuth);

router.get("/:agent", asyncHandler(listChatSessionsHandler));
router.post("/:agent", asyncHandler(saveChatSessionHandler));
router.get("/:agent/:id", asyncHandler(getChatSessionHandler));
router.delete("/:agent/:id", asyncHandler(deleteChatSessionHandler));

export default router;
