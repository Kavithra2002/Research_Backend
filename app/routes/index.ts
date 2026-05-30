import { Router } from "express";
import mongoose from "mongoose";
import authRoutes from "./authRoutes";
import userRoutes from "./userRoutes";
import reportSelectionRoutes from "./reportSelectionRoutes";
import companyGroupRoutes from "./companyGroupRoutes";
import loginHistoryRoutes from "./loginHistoryRoutes";
import sageAgentRoutes from "./sageAgentRoutes";
import systemPythonRoutes from "./systemPythonRoutes";

const router = Router();

router.get("/health", (_req, res) => {
  const dbState = mongoose.connection.readyState;
  const dbStateName =
    ({ 0: "disconnected", 1: "connected", 2: "connecting", 3: "disconnecting" } as const)[
      dbState as 0 | 1 | 2 | 3
    ] ?? "unknown";

  res.json({
    status: "ok",
    uptime: process.uptime(),
    timestamp: new Date().toISOString(),
    db: {
      state: dbState,
      stateName: dbStateName,
      host: mongoose.connection.host ?? null,
      name: mongoose.connection.name ?? null,
    },
  });
});

router.use("/auth", authRoutes);
router.use("/users", userRoutes);
router.use("/report-selections", reportSelectionRoutes);
router.use("/company-groups", companyGroupRoutes);
router.use("/login-history", loginHistoryRoutes);
router.use("/ai/sage", sageAgentRoutes);
router.use(systemPythonRoutes);

export default router;
