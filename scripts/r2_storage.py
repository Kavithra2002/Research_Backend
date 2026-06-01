"""
Cloudflare R2 (S3-compatible) storage helpers for Python scripts.

When STORAGE_DRIVER=r2 (or s3), scripts upload/download objects using the
same key layout as the Next.js storage layer:

    updated_reports/<company>/<reportType>/<file>.pdf
    testing/<company>/...
    reports/<company>/...

Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, and R2_BUCKET
in the environment (same vars as the Node backend / Vercel frontend).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover
    boto3 = None  # type: ignore[assignment]
    ClientError = Exception  # type: ignore[assignment,misc]

UPDATED_REPORTS_PREFIX = "updated_reports"
NEWLY_UPLOADED_PREFIX = "newly_uploaded_report"  # legacy prefix
TESTING_PREFIX = "testing"
REPORTS_PREFIX = "reports"

_client: Any = None


def is_r2_enabled() -> bool:
    driver = (os.environ.get("STORAGE_DRIVER") or "local").strip().lower()
    return driver in ("r2", "s3")


def _require_boto3() -> None:
    if boto3 is None:
        raise RuntimeError(
            "boto3 is required for R2 storage. "
            "Install with: pip install boto3"
        )


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client

    _require_boto3()

    account_id = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
    access_key = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    secret_key = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
    if not account_id or not access_key or not secret_key:
        raise RuntimeError(
            "R2 credentials missing. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "and R2_SECRET_ACCESS_KEY."
        )

    endpoint = (os.environ.get("R2_ENDPOINT") or "").strip()
    if not endpoint:
        endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

    _client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    return _client


def get_bucket() -> str:
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    if not bucket:
        raise RuntimeError("R2_BUCKET is not configured")
    return bucket


def object_key(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p)


def updated_report_key(company: str, report_type: str, file_name: str) -> str:
    return object_key(UPDATED_REPORTS_PREFIX, company, report_type, file_name)


def upload_file(local_path: Path, key: str) -> None:
    """Upload a local file to R2."""
    if not is_r2_enabled():
        return
    client = _get_client()
    bucket = get_bucket()
    client.upload_file(str(local_path), bucket, key)


def download_file(key: str, local_path: Path) -> bool:
    """Download an R2 object to a local path. Returns True on success."""
    if not is_r2_enabled():
        return False
    client = _get_client()
    bucket = get_bucket()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        client.download_file(bucket, key, str(local_path))
        return local_path.exists() and local_path.stat().st_size > 0
    except ClientError as ex:
        code = ex.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def object_exists(key: str) -> bool:
    if not is_r2_enabled():
        return False
    client = _get_client()
    bucket = get_bucket()
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as ex:
        code = ex.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def upload_directory(local_dir: Path, prefix: str) -> int:
    """Upload every file under local_dir to R2 with the given prefix. Returns count."""
    if not is_r2_enabled():
        return 0
    if not local_dir.is_dir():
        return 0

    count = 0
    for path in local_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        key = object_key(prefix, rel)
        upload_file(path, key)
        count += 1
    return count


def ensure_updated_report_local(
    company: str,
    report_type: str,
    file_name: str,
    local_root: Path,
) -> Path | None:
    """
    Ensure a report PDF exists locally, downloading from R2 if needed.
    Returns the local path or None if unavailable.
    """
    local_path = local_root / company / report_type / file_name
    if local_path.exists() and local_path.stat().st_size > 1024:
        return local_path

    if not is_r2_enabled():
        return local_path if local_path.exists() else None

    key = updated_report_key(company, report_type, file_name)
    if download_file(key, local_path):
        return local_path

    # Fallback: legacy prefix
    legacy_key = object_key(NEWLY_UPLOADED_PREFIX, company, report_type, file_name)
    if download_file(legacy_key, local_path):
        return local_path

    return None
