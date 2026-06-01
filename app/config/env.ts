import "dotenv/config";

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
  },
} as const;

export type Env = typeof env;
