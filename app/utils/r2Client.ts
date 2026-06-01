import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {
  GetObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";

let client: S3Client | null = null;

function isR2Enabled(): boolean {
  const driver = (process.env.STORAGE_DRIVER ?? "local").trim().toLowerCase();
  return driver === "r2" || driver === "s3";
}

function getClient(): S3Client {
  if (client) return client;
  const accountId = process.env.R2_ACCOUNT_ID?.trim();
  const accessKeyId = process.env.R2_ACCESS_KEY_ID?.trim();
  const secretAccessKey = process.env.R2_SECRET_ACCESS_KEY?.trim();
  if (!accountId || !accessKeyId || !secretAccessKey) {
    throw new Error("R2 credentials are not configured on the backend");
  }
  const endpoint =
    process.env.R2_ENDPOINT?.trim() ||
    `https://${accountId}.r2.cloudflarestorage.com`;
  client = new S3Client({
    region: "auto",
    endpoint,
    credentials: { accessKeyId, secretAccessKey },
  });
  return client;
}

function getBucket(): string {
  const bucket = process.env.R2_BUCKET?.trim();
  if (!bucket) throw new Error("R2_BUCKET is not configured");
  return bucket;
}

/** If pdfPath is an R2 object key, download to /tmp and return local path. */
export async function resolveLocalPdfPath(pdfPath: string): Promise<string> {
  const trimmed = pdfPath.trim();
  if (!trimmed) return trimmed;

  const looksLikeKey =
    trimmed.startsWith("reports/") ||
    trimmed.startsWith("newly_uploaded_report/") ||
    trimmed.startsWith("updated_reports/");

  if (!looksLikeKey || !isR2Enabled()) {
    return trimmed;
  }

  const res = await getClient().send(
    new GetObjectCommand({ Bucket: getBucket(), Key: trimmed }),
  );
  if (!res.Body) {
    throw new Error(`R2 object not found: ${trimmed}`);
  }
  const bytes = await res.Body.transformToByteArray();
  const base = path.basename(trimmed);
  const tmp = path.join(
    os.tmpdir(),
    `ambeon-pdf-${Date.now()}-${process.pid}-${base}`,
  );
  await fs.writeFile(tmp, bytes);
  return tmp;
}
