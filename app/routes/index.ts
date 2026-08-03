import { Router } from "express";
import mongoose from "mongoose";
import authRoutes from "./authRoutes";
import userRoutes from "./userRoutes";
import reportSelectionRoutes from "./reportSelectionRoutes";
import companyGroupRoutes from "./companyGroupRoutes";
import loginHistoryRoutes from "./loginHistoryRoutes";
import sageAgentRoutes from "./sageAgentRoutes";
import robinAgentRoutes from "./robinAgentRoutes";
import tuckAgentRoutes from "./tuckAgentRoutes";
import marianAgentRoutes from "./marianAgentRoutes";
import joneAgentRoutes from "./joneAgentRoutes";
import reportScheduleRoutes from "./reportScheduleRoutes";
import ragRoutes from "./ragRoutes";
import chatLogRoutes from "./chatLogRoutes";
import systemPythonRoutes from "./systemPythonRoutes";
import extractedRoutes from "./extractedRoutes";
import openaiUsageRoutes from "./openaiUsageRoutes";
import newspaperRoutes from "./newspaperRoutes";
import dbRoutes from "./dbRoutes";
import extractionKeywordRoutes from "./extractionKeywordRoutes";
import cseRoutes from "./cseRoutes";

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
router.use("/ai/robin", robinAgentRoutes);
router.use("/ai/tuck", tuckAgentRoutes);
router.use("/ai/marian", marianAgentRoutes);
router.use("/ai/jone", joneAgentRoutes);
router.use("/ai/report-schedule", reportScheduleRoutes);
router.use("/ai/rag", ragRoutes);
router.use("/ai/chat-log", chatLogRoutes);
router.use("/openai", openaiUsageRoutes);
router.use("/extracted", extractedRoutes);
router.use("/newspaper", newspaperRoutes);
router.use("/db", dbRoutes);
router.use("/extraction-keywords", extractionKeywordRoutes);
router.use("/cse", cseRoutes);
router.use(systemPythonRoutes);

export default router;
