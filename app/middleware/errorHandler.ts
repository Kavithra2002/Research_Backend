import type { ErrorRequestHandler, RequestHandler } from "express";
import { Error as MongooseError } from "mongoose";
import { HttpError } from "../utils/httpError";
import { logger } from "../utils/logger";

export const notFoundHandler: RequestHandler = (req, res) => {
  res.status(404).json({
    error: "Not Found",
    message: `Route ${req.method} ${req.originalUrl} does not exist`,
  });
};

export const errorHandler: ErrorRequestHandler = (err, _req, res, _next) => {
  if (err instanceof HttpError) {
    res.status(err.status).json({
      error: err.name,
      message: err.message,
      details: err.details,
    });
    return;
  }

  if (err instanceof MongooseError.ValidationError) {
    res.status(400).json({
      error: "ValidationError",
      message: err.message,
      details: Object.fromEntries(
        Object.entries(err.errors).map(([k, v]) => [k, v.message]),
      ),
    });
    return;
  }

  if (err instanceof MongooseError.CastError) {
    res.status(400).json({
      error: "CastError",
      message: `Invalid value for path "${err.path}": ${String(err.value)}`,
    });
    return;
  }

  if (
    typeof err === "object" &&
    err !== null &&
    "code" in err &&
    (err as { code?: number }).code === 11000
  ) {
    res.status(409).json({
      error: "DuplicateKey",
      message: "A document with the same unique key already exists",
      details: (err as { keyValue?: unknown }).keyValue,
    });
    return;
  }

  logger.error("Unhandled error", err);
  res.status(500).json({
    error: "InternalServerError",
    message:
      err instanceof Error ? err.message : "Something went wrong on the server",
  });
};
