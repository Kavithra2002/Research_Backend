import mongoose from "mongoose";
import { env } from "./env";
import { logger } from "../utils/logger";

mongoose.set("strictQuery", true);

let connectionPromise: Promise<typeof mongoose> | null = null;

export async function connectToDatabase(): Promise<typeof mongoose> {
  if (mongoose.connection.readyState === 1) {
    return mongoose;
  }

  if (!connectionPromise) {
    logger.info(
      `Connecting to MongoDB at ${env.mongoUri} (db: ${env.mongoDbName})`,
    );
    connectionPromise = mongoose
      .connect(env.mongoUri, {
        dbName: env.mongoDbName,
        serverSelectionTimeoutMS: 10_000,
      })
      .then((conn) => {
        logger.info(
          `MongoDB connected: host=${conn.connection.host} db=${conn.connection.name}`,
        );
        return conn;
      })
      .catch((err) => {
        connectionPromise = null;
        throw err;
      });
  }

  return connectionPromise;
}

export async function disconnectFromDatabase(): Promise<void> {
  if (mongoose.connection.readyState === 0) return;
  await mongoose.disconnect();
  connectionPromise = null;
  logger.info("MongoDB disconnected");
}

mongoose.connection.on("error", (err) => {
  logger.error("MongoDB connection error", err);
});

mongoose.connection.on("disconnected", () => {
  logger.warn("MongoDB disconnected event received");
});
