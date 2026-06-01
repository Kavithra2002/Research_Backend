import { createApp } from "./app";
import { env } from "./config/env";
import { connectToDatabase, disconnectFromDatabase } from "./config/db";
import { killAllPythonJobs } from "./routes/systemPythonRoutes";
import { logger } from "./utils/logger";

async function bootstrap() {
  try {
    await connectToDatabase();
    console.log(
      `[DB] DB connected successfully  (host=localhost, db=${env.mongoDbName})`,
    );
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    console.error(`[DB] DB is not connected  (${reason})`);
    logger.error("Failed to connect to MongoDB. Exiting.", err);
    process.exit(1);
  }

  const app = createApp();
  const server = app.listen(env.port, () => {
    logger.info(`API server listening on http://localhost:${env.port}`);
    logger.info(`Health check available at http://localhost:${env.port}/api/health`);
  });

  const shutdown = async (signal: string) => {
    logger.info(`Received ${signal}, shutting down...`);
    killAllPythonJobs();
    server.close(() => logger.info("HTTP server closed"));
    try {
      await disconnectFromDatabase();
    } catch (err) {
      logger.error("Error while disconnecting MongoDB", err);
    }
    process.exit(0);
  };

  process.on("SIGINT", () => void shutdown("SIGINT"));
  process.on("SIGTERM", () => void shutdown("SIGTERM"));
  process.on("unhandledRejection", (reason) => {
    logger.error("Unhandled promise rejection", reason);
  });
  process.on("uncaughtException", (err) => {
    logger.error("Uncaught exception", err);
  });
}

void bootstrap();
