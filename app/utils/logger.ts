type LogLevel = "info" | "warn" | "error" | "debug";

function timestamp(): string {
  return new Date().toISOString();
}

function format(level: LogLevel, message: unknown, meta?: unknown): string {
  const prefix = `[${timestamp()}] [${level.toUpperCase()}]`;
  if (meta === undefined) return `${prefix} ${stringify(message)}`;
  return `${prefix} ${stringify(message)} ${stringify(meta)}`;
}

function stringify(value: unknown): string {
  if (value instanceof Error) return value.stack ?? value.message;
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export const logger = {
  info(message: unknown, meta?: unknown) {
    console.log(format("info", message, meta));
  },
  warn(message: unknown, meta?: unknown) {
    console.warn(format("warn", message, meta));
  },
  error(message: unknown, meta?: unknown) {
    console.error(format("error", message, meta));
  },
  debug(message: unknown, meta?: unknown) {
    if (process.env.NODE_ENV === "development") {
      console.debug(format("debug", message, meta));
    }
  },
};
