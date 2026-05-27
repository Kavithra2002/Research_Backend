import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  addSelectionHandler,
  clearSelectionsHandler,
  listSelectionsHandler,
  removeSelectionHandler,
  replaceSelectionsHandler,
} from "../controllers/reportSelectionController";

const router = Router();

router.use(requireAuth);

router.get("/", asyncHandler(listSelectionsHandler));
router.post("/", asyncHandler(addSelectionHandler));
router.put("/", asyncHandler(replaceSelectionsHandler));
router.delete("/", asyncHandler(clearSelectionsHandler));
router.delete("/item", asyncHandler(removeSelectionHandler));

export default router;
