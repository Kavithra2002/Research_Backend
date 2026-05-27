import type { NextFunction, Request, Response } from "express";
import { SESSION_COOKIE_NAME } from "../controllers/authController";
import { getUserBySession, type AuthenticatedUser } from "../services/authService";

declare module "express-serve-static-core" {
  interface Request {
    user?: AuthenticatedUser;
  }
}

export async function requireAuth(
  req: Request,
  res: Response,
  next: NextFunction,
) {
  const sid =
    (req.cookies as Record<string, string | undefined> | undefined)?.[
      SESSION_COOKIE_NAME
    ] ?? "";

  const user = await getUserBySession(sid);
  if (!user) {
    res.status(401).json({ error: "Unauthorized", message: "Not signed in" });
    return;
  }

  req.user = user;
  next();
}
