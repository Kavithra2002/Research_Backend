import { Schema, model, type HydratedDocument } from "mongoose";

export type ReportAgent = "robin" | "tuck" | "marian";
export type ReportFrequency = "once" | "daily" | "weekly" | "monthly";

export interface ReportScheduleAttrs {
  user_id: string;
  /** Which agent this schedule drives. */
  agent: ReportAgent;
  /** 24h time string, e.g. "09:00". */
  time: string;
  /** ISO date string "YYYY-MM-DD". One-time date, or start date for recurring. */
  date: string;
  /** How often the report runs. */
  frequency: ReportFrequency;
  /** Recipient emails the generated report is sent to. */
  emails: string[];
  /**
   * Snapshot of the agent configuration needed to regenerate the report:
   *  • robin  → { companies: string[]; metrics: string[] }
   *  • tuck   → { sections: string[]; companies: string[] }
   *  • marian → { sections: string[] }
   */
  config: Record<string, unknown>;
  /** Whether this schedule is active. */
  enabled: boolean;
  /** Timestamp of the last successful run (drives next-run gating). */
  lastRunAt: Date | null;
}

const reportScheduleSchema = new Schema<ReportScheduleAttrs>(
  {
    user_id: { type: String, required: true, index: true },
    agent: { type: String, enum: ["robin", "tuck", "marian"], required: true },
    time: { type: String, default: "09:00" },
    date: { type: String, default: "" },
    frequency: {
      type: String,
      enum: ["once", "daily", "weekly", "monthly"],
      default: "once",
    },
    emails: { type: [String], default: [] },
    config: { type: Schema.Types.Mixed, default: {} },
    enabled: { type: Boolean, default: true },
    lastRunAt: { type: Date, default: null },
  },
  { collection: "report_schedules", versionKey: false, timestamps: true },
);

reportScheduleSchema.index({ user_id: 1, agent: 1 }, { unique: true });

export type ReportScheduleDocument = HydratedDocument<ReportScheduleAttrs>;

export const ReportSchedule = model<ReportScheduleAttrs>(
  "ReportSchedule",
  reportScheduleSchema,
);
