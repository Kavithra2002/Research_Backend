import type { NextFunction, Request, Response } from "express";
import type { UserRole } from "../models/User";

export function requireRole(...roles: UserRole[]) {
  return function roleGuard(req: Request, res: Response, next: NextFunction) {
    const user = req.user;
    if (!user) {
      res
        .status(401)
        .json({ error: "Unauthorized", message: "Not signed in" });
      return;
    }
    if (!roles.includes(user.role as UserRole)) {
      res.status(403).json({
        error: "Forbidden",
        message: "You do not have permission to perform this action",
      });
      return;
    }
    next();
  };
}
