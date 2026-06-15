import "dotenv/config";
import path from "node:path";

function readString(key: string, fallback: string): string {
  const value = process.env[key];
  if (value === undefined || value.trim() === "") return fallback;
  return value.trim();
}

function readNumber(key: string, fallback: number): number {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === "") return fallback;
  const parsed = Number(raw);
  if (Number.isNaN(parsed)) {
    throw new Error(`Environment variable ${key} must be a number, got "${raw}"`);
  }
  return parsed;
}

function readOptional(key: string): string | null {
  const value = process.env[key];
  if (value === undefined || value.trim() === "") return null;
  return value.trim();
}

function readBool(key: string, fallback: boolean): boolean {
  const raw = process.env[key];
  if (raw === undefined || raw.trim() === "") return fallback;
  const normalized = raw.trim().toLowerCase();
  if (["true", "1", "yes", "on"].includes(normalized)) return true;
  if (["false", "0", "no", "off"].includes(normalized)) return false;
  return fallback;
}

export const env = {
  nodeEnv: readString("NODE_ENV", "development"),
  port: readNumber("PORT", 4000),
  corsOrigin: readString("CORS_ORIGIN", "http://localhost:3000"),
  frontendUrl: readString(
    "FRONTEND_URL",
    readString("CORS_ORIGIN", "http://localhost:3000"),
  ),
  mongoUri: readString("MONGO_URI", "mongodb://localhost:27017"),
  mongoDbName: readString("MONGO_DB_NAME", "Research_Project"),
  smtp: {
    host: readOptional("SMTP_HOST"),
    port: readNumber("SMTP_PORT", 587),
    secure: readBool("SMTP_SECURE", false),
    user: readOptional("SMTP_USER"),
    pass: readOptional("SMTP_PASS"),
    from: readOptional("SMTP_FROM"),
  },
  /** Prefer Resend on Render — Gmail SMTP is often blocked from cloud hosts. */
  resend: {
    apiKey: readOptional("RESEND_API_KEY"),
    from: readOptional("RESEND_FROM"),
  },
  openai: {
    apiKey: readOptional("OPENAI_API_KEY"),
    model: readString("OPENAI_CHAT_MODEL", "gpt-4o-mini"),
    /**
     * Embedding model used by the RAG vector store. text-embedding-3-small is
     * cheap (1536 dims) and works well for English report text.
     */
    embedModel: readString("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
    /**
     * Organization Admin key (sk-admin-...) used only for the read-only
     * Costs API that powers the sidebar spend widget. Distinct from the
     * regular OPENAI_API_KEY — the Costs endpoint rejects normal secret keys.
     */
    adminKey: readOptional("OPENAI_ADMIN_KEY"),
    /**
     * Manual credit-balance snapshot. OpenAI exposes no API for the live
     * balance, so the admin records the dashboard balance here along with the
     * date it was true; the widget subtracts real spend since that date to
     * show a live "remaining" estimate. Re-set after each top-up.
     */
    creditBalance: readOptional("OPENAI_CREDIT_BALANCE"),
    creditBalanceAsOf: readOptional("OPENAI_CREDIT_BALANCE_AS_OF"),
  },
  /**
   * Where agent chat histories are stored on disk. Defaults to the
   * `Agent_chat_log/` folder at the project root (one level up from the
   * backend, which is the working directory when `npm run dev` runs).
   */
  chatLogDir: readString(
    "AGENT_CHAT_LOG_DIR",
    path.resolve(process.cwd(), "..", "Agent_chat_log"),
  ),
  /**
   * Folder holding the demo company report archive (one sub-folder per
   * company). Robin's configuration panel lists these companies. Defaults to
   * the `Demo_Data/` folder inside the backend working directory.
   */
  demoDataDir: readString(
    "DEMO_DATA_DIR",
    path.resolve(process.cwd(), "Demo_Data"),
  ),
} as const;

export type Env = typeof env;
