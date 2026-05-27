import { Router } from "express";
import { asyncHandler } from "../utils/asyncHandler";
import {
  forgotPasswordHandler,
  loginHandler,
  logoutHandler,
  meHandler,
  resetPasswordHandler,
  signupHandler,
} from "../controllers/authController";

const router = Router();

router.post("/signup", asyncHandler(signupHandler));
router.post("/login", asyncHandler(loginHandler));
router.post("/logout", asyncHandler(logoutHandler));
router.post("/forgot-password", asyncHandler(forgotPasswordHandler));
router.post("/reset-password", asyncHandler(resetPasswordHandler));
router.get("/me", asyncHandler(meHandler));

export default router;
