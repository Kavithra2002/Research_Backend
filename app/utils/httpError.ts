export class HttpError extends Error {
  public readonly status: number;
  public readonly details?: unknown;

  constructor(status: number, message: string, details?: unknown) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.details = details;
  }

  static badRequest(message = "Bad Request", details?: unknown) {
    return new HttpError(400, message, details);
  }

  static notFound(message = "Not Found", details?: unknown) {
    return new HttpError(404, message, details);
  }

  static conflict(message = "Conflict", details?: unknown) {
    return new HttpError(409, message, details);
  }

  static internal(message = "Internal Server Error", details?: unknown) {
    return new HttpError(500, message, details);
  }
}
