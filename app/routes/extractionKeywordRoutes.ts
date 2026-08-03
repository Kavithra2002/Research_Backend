import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import {
  addNewKeywordHandler,
  addSimilarKeywordHandler,
  deleteKeywordHandler,
  listKeywordsHandler,
  listSheetLinesHandler,
  removeUserAliasHandler,
  reorderKeywordHandler,
  seedKeywordsHandler,
  suggestCanonicalLabelsHandler,
} from "../controllers/extractionKeywordController";

const router = Router();

router.use(requireAuth);

router.get("/", asyncHandler(listKeywordsHandler));
router.get("/line-order", asyncHandler(listSheetLinesHandler));
router.post("/seed", asyncHandler(seedKeywordsHandler));
router.get("/suggest", asyncHandler(suggestCanonicalLabelsHandler));
router.post("/new", asyncHandler(addNewKeywordHandler));
router.post("/similar", asyncHandler(addSimilarKeywordHandler));
router.patch("/:id/reorder", asyncHandler(reorderKeywordHandler));
router.post("/:id/remove-alias", asyncHandler(removeUserAliasHandler));
router.delete("/:id", asyncHandler(deleteKeywordHandler));

export default router;
