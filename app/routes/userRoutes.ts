import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import { requireAuth } from "../middleware/requireAuth";
import { requireRole } from "../middleware/requireRole";
import {
  createUserHandler,
  deleteUserHandler,
  getUserHandler,
  listUsersHandler,
  recordLoginHandler,
  updateUserHandler,
} from "../controllers/userController";

const router = Router();

router.use(requireAuth, requireRole("Admin"));

router.get("/", asyncHandler(listUsersHandler));
router.post("/", asyncHandler(createUserHandler));
router.get("/:id", asyncHandler(getUserHandler));
router.patch("/:id", asyncHandler(updateUserHandler));
router.delete("/:id", asyncHandler(deleteUserHandler));
router.post("/:id/login", asyncHandler(recordLoginHandler));

export default router;
