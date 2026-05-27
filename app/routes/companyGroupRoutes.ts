import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  createGroupHandler,
  deleteGroupHandler,
  getGroupHandler,
  listGroupsHandler,
  updateGroupHandler,
} from "../controllers/companyGroupController";

const router = Router();

router.use(requireAuth);

router.get("/", asyncHandler(listGroupsHandler));
router.post("/", asyncHandler(createGroupHandler));
router.get("/:id", asyncHandler(getGroupHandler));
router.put("/:id", asyncHandler(updateGroupHandler));
router.delete("/:id", asyncHandler(deleteGroupHandler));

export default router;
